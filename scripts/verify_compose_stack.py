"""Verify only compose.ollama.yaml's local stack, including optional service restarts.

Uses small real Ollama calls. Redis markers use a unique namespace, never FLUSHDB.
--restart restarts only this project's gateway, PostgreSQL, and Redis services.
Does not change the Windows PostgreSQL services, Ollama, or other Compose projects.
"""

import argparse
import asyncio
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from verify_live_gateway import verify

from gateway.schemas import TeamPolicy
from gateway.state import RedisStore

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8080"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "compose.ollama.yaml")]


async def wait_ready(client, url, timeout=90):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            response = await client.get(url, timeout=3)
            if response.status_code == 200:
                return response
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Service did not become ready: {url}")


async def compose(*arguments):
    result = await asyncio.to_thread(
        subprocess.run,
        [*COMPOSE, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"Compose command failed: {' '.join(arguments)}\n{result.stderr}")
    return result.stdout


async def main(args):
    admin_key = os.environ.get("GATEWAY_ADMIN_KEY", "demo-admin-key-change-me")
    admin = {"Authorization": "Bearer " + admin_key}
    redis_url = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:56379/0")
    namespace = "stack-durability-" + uuid.uuid4().hex
    state = RedisStore(Redis.from_url(redis_url), namespace)
    policy = TeamPolicy(
        allowed_models=["balanced"],
        requests_per_minute=1,
        tokens_per_minute=1000,
        daily_budget_microusd=1000,
    )
    report = {"timestamp_utc": datetime.now(UTC).isoformat(), "restart_requested": args.restart}
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            await wait_ready(client, BASE_URL + "/health/ready")
            await verify(
                argparse.Namespace(
                    url=BASE_URL,
                    provider="local",
                    output=ROOT / "artifacts/compose-ollama-verification.json",
                )
            )
            proof = json.loads(
                (ROOT / "artifacts/compose-ollama-verification.json").read_text(encoding="utf-8")
            )
            ids = {proof["normal"]["id"], proof["stream"]["request_id"]}

            async def check_audit():
                response = await client.get(BASE_URL + "/admin/audit", headers=admin)
                response.raise_for_status()
                records = [r for r in response.json()["events"] if r["request_id"] in ids]
                assert len(records) == 4, "Missing committed request/attempt audit records"
                return len(records)

            report["audit_rows_before_restart"] = await check_audit()
            await state.seed_policy("probe", policy)
            reservation = await state.reserve("probe", policy, 100)
            assert await state.settle(reservation, 12)
            assert await state.spent("probe") == 12

            if args.restart:
                print("Checking gateway restart...", flush=True)
                await compose("restart", "gateway")
                await wait_ready(client, BASE_URL + "/health/ready")
                report["audit_rows_after_gateway_restart"] = await check_audit()

                print("Checking PostgreSQL restart...", flush=True)
                await compose("restart", "postgres")
                await wait_ready(client, BASE_URL + "/health/ready")
                report["audit_rows_after_postgres_restart"] = await check_audit()

                print("Checking Redis restart and AOF recovery...", flush=True)
                await compose("restart", "redis")
                await wait_ready(client, BASE_URL + "/health/ready")
                await state.close()
                state = RedisStore(Redis.from_url(redis_url), namespace)
                assert (await state.policy("probe")) == policy
                assert await state.spent("probe") == 12
                report["redis_policy_and_spending_after_server_restart"] = "passed"

            denied = await client.post(
                BASE_URL + "/v1/chat/completions",
                json={"model": "balanced", "messages": [{"role": "user", "content": "Hi"}]},
            )
            assert denied.status_code == 401
            report["missing_credential_status"] = denied.status_code

            await wait_ready(client, "http://127.0.0.1:9091/-/ready")
            # A target can be ready before the first 5-second scrape has completed.
            targets = []
            for _ in range(30):
                response = await client.get("http://127.0.0.1:9091/api/v1/targets")
                response.raise_for_status()
                targets = response.json()["data"]["activeTargets"]
                if any(
                    t["labels"].get("job") == "llm-gateway" and t["health"] == "up" for t in targets
                ):
                    break
                await asyncio.sleep(0.5)
            assert any(
                t["labels"].get("job") == "llm-gateway" and t["health"] == "up" for t in targets
            )
            report["prometheus_gateway_target"] = "up"
            rules = await client.get("http://127.0.0.1:9091/api/v1/rules")
            rules.raise_for_status()
            alert_rules = [r for g in rules.json()["data"]["groups"] for r in g["rules"]]
            assert alert_rules and all(r.get("health") == "ok" for r in alert_rules)
            report["prometheus_rule_count"] = len(alert_rules)

            grafana = await wait_ready(client, "http://127.0.0.1:3001/api/health")
            assert grafana.json()["database"] == "ok"
            dashboards = await client.get(
                "http://127.0.0.1:3001/api/search",
                params={"type": "dash-db"},
                auth=httpx.BasicAuth("admin", "demo-password-change-me"),
            )
            dashboards.raise_for_status()
            titles = {d["title"] for d in dashboards.json()}
            assert {"Gateway Business", "Gateway Operations", "Gateway Performance"} <= titles
            report["grafana_dashboards"] = sorted(titles)
            await wait_ready(client, "http://127.0.0.1:9094/-/ready")
            report["alertmanager"] = "ready"

            # This checks receipt, not searchable trace retention or individual span content.
            collector_log = await compose("logs", "--no-color", "--tail", "200", "otel")
            assert '"spans"' in collector_log or "resource spans" in collector_log.lower()
            report["collector_received_spans"] = True
            report["request_ids"] = sorted(ids)
            report["result"] = "passed"
            output = ROOT / "artifacts/compose-stack-verification.json"
            output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2))
    finally:
        # Clean up only our own marker. Never remove application quota or budget keys.
        try:
            keys = [key async for key in state.client.scan_iter(match=state.key("probe", "*"))]
            if keys:
                await state.client.delete(*keys)
        except RedisError:
            print(
                "Redis unavailable during test-marker cleanup; inspect the unique test namespace."
            )
        finally:
            await state.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--restart", action="store_true", help="Restart only this project's services"
    )
    asyncio.run(main(parser.parse_args()))
