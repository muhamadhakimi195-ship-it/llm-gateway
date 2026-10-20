"""Verify real Ollama calls and PostgreSQL persistence across an API process restart.

Owns only child gateway processes started by this script, on a separate port.
Does not stop the user's gateway, PostgreSQL, Redis, or Ollama. Set DATABASE_URL.
Optionally set TEST_REDIS_URL to also verify a quota survives the API restart.
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from verify_live_gateway import verify

ROOT = Path(__file__).resolve().parents[1]


async def wait_ready(client, url, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Child gateway exited; check artifacts/restart-gateway.log")
        try:
            if (await client.get(url + "/health/ready", timeout=1)).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError("Child gateway did not become ready")


def start_server(port, environment, log):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gateway.cli",
            "serve",
            "--config",
            str(ROOT / "config/ollama.yaml"),
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
        **options,
    )


def stop_server(process):
    # Only terminate our own child. Committed audit rows must survive even this exit.
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def main(args):
    if not os.environ.get("DATABASE_URL"):
        raise ValueError("Set DATABASE_URL to the dedicated, migrated PostgreSQL database")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))  # Fail rather than stop an existing service.
    url = f"http://127.0.0.1:{args.port}"
    environment = os.environ.copy()
    environment.update(
        GATEWAY_AUDIT_BACKEND="postgres",
        GATEWAY_STATE_BACKEND="redis" if os.environ.get("TEST_REDIS_URL") else "memory",
        GATEWAY_NAMESPACE="restart-verification-" + uuid.uuid4().hex,
    )
    environment.setdefault("GATEWAY_ADMIN_KEY", "demo-admin-key-change-me")
    environment.setdefault("DEMO_TEAM_KEY", "demo-team-key-change-me")
    environment.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    if os.environ.get("TEST_REDIS_URL"):
        environment["REDIS_URL"] = os.environ["TEST_REDIS_URL"]
    admin = {"Authorization": "Bearer " + environment["GATEWAY_ADMIN_KEY"]}
    team = {"Authorization": "Bearer " + environment["DEMO_TEAM_KEY"]}
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    process = None
    with (output / "restart-gateway.log").open("a", encoding="utf-8") as log:
        async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
            try:
                process = start_server(args.port, environment, log)
                await wait_ready(client, url, process)
                await verify(
                    argparse.Namespace(
                        url=url,
                        provider="local",
                        output=output / "ollama-postgres-verification.json",
                    )
                )
                proof = json.loads(
                    (output / "ollama-postgres-verification.json").read_text(encoding="utf-8")
                )
                ids = {proof["normal"]["id"], proof["stream"]["request_id"]}
                before = await client.get(url + "/admin/audit", headers=admin)
                before.raise_for_status()
                before_ids = {r["request_id"] for r in before.json()["events"]}
                assert ids <= before_ids, "Real requests are missing from PostgreSQL audit"
                quota_started = None
                if environment["GATEWAY_STATE_BACKEND"] == "redis":
                    response = await client.get(url + "/admin/teams/demo", headers=admin)
                    response.raise_for_status()
                    policy = response.json()["policy"]
                    policy["requests_per_minute"] = 1
                    changed = await client.put(
                        url + "/admin/teams/demo/policy", headers=admin, json=policy
                    )
                    changed.raise_for_status()
                    # Consume one allowance with a deliberately small real generation.
                    # An unknown alias would be rejected before quota admission.
                    response = await client.post(
                        url + "/v1/chat/completions",
                        headers=team,
                        json={
                            "model": "balanced",
                            "max_tokens": 8,
                            "messages": [{"role": "user", "content": "Say hello."}],
                        },
                    )
                    response.raise_for_status()
                    quota_started = time.monotonic()
                first_pid = process.pid
                stop_server(process)
                process = start_server(args.port, environment, log)
                await wait_ready(client, url, process)
                after = await client.get(url + "/admin/audit", headers=admin)
                after.raise_for_status()
                events = [r for r in after.json()["events"] if r["request_id"] in ids]
                assert {r["request_id"] for r in events} == ids
                assert len(events) == 4, "Expected a request and attempt audit for each real call"
                quota_status = None
                if quota_started is not None:
                    assert time.monotonic() - quota_started < 45, (
                        "Quota test exceeded refill window"
                    )
                    response = await client.post(
                        url + "/v1/chat/completions",
                        headers=team,
                        json={
                            "model": "balanced",
                            "max_tokens": 8,
                            "messages": [{"role": "user", "content": "Say hello."}],
                        },
                    )
                    quota_status = response.status_code
                    assert quota_status == 429, "Restart reset the Redis rate allowance"
                report = {
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "first_pid": first_pid,
                    "restarted_pid": process.pid,
                    "request_ids": sorted(ids),
                    "audit_rows_after_restart": len(events),
                    "postgres_persistence": "passed",
                    "state_backend": environment["GATEWAY_STATE_BACKEND"],
                    "redis_quota_status_after_restart": quota_status,
                    "redis_note": "API restart only; not Redis server/AOF recovery",
                }
                (output / "restart-verification.json").write_text(
                    json.dumps(report, indent=2) + "\n", encoding="utf-8"
                )
                print(json.dumps(report, indent=2))
            finally:
                stop_server(process)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    asyncio.run(main(parser.parse_args()))
