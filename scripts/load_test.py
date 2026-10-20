"""Measure a real HTTP mock workload, recording failures as well as successful latency.

This is a gateway/infrastructure benchmark, not evidence about model quality or
paid-provider latency. It refuses non-loopback hosts to avoid unintended load.
"""

import argparse
import asyncio
import json
import math
import os
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


async def benchmark(args):
    if urlsplit(args.url).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Load tests are restricted to loopback URLs")
    if not 1 <= args.requests <= 20_000 or not 1 <= args.concurrency <= 128:
        raise ValueError("requests must be 1..20000; concurrency must be 1..128")
    team_keys = {
        "demo": os.environ.get("DEMO_TEAM_KEY", "demo-team-key-change-me"),
        "analyst": os.environ.get("ANALYST_TEAM_KEY", "demo-analyst-key-change-me"),
    }
    admin = {
        "Authorization": "Bearer " + os.environ.get("GATEWAY_ADMIN_KEY", "demo-admin-key-change-me")
    }
    statuses = Counter()
    all_latencies, success_latencies = [], []
    budget = 0
    costs_by_team = Counter()
    requests_by_team = Counter()
    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(
        timeout=60, trust_env=False, limits=httpx.Limits(max_connections=args.concurrency)
    ) as client:

        async def spending():
            values = {}
            for team in team_keys:
                response = await client.get(args.url + f"/admin/teams/{team}", headers=admin)
                response.raise_for_status()
                values[team] = response.json()["spent_microusd"]
            return values

        before = await spending()

        async def request(number):
            nonlocal budget
            team = "analyst" if number % 50 == 0 else "demo"
            headers = {"Authorization": "Bearer " + team_keys[team]}
            requests_by_team[team] += 1
            body = {
                "model": "balanced",
                "max_tokens": 32,
                "priority": "batch" if number % 5 == 0 else "interactive",
                "messages": [
                    {"role": "user", "content": f"Explain gateway reliability example {number}."}
                ],
            }
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.post(
                        args.url + "/v1/chat/completions", headers=headers, json=body
                    )
                    elapsed = (time.perf_counter() - started) * 1000
                    all_latencies.append(elapsed)
                    statuses[str(response.status_code)] += 1
                    if response.status_code == 200:
                        success_latencies.append(elapsed)
                        budget += response.json()["gateway"]["accounted_microusd"]
                        costs_by_team[team] += response.json()["gateway"]["accounted_microusd"]
                except httpx.HTTPError:
                    statuses["transport_error"] += 1
                    all_latencies.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        await asyncio.gather(*(request(i) for i in range(args.requests)))
        wall_seconds = time.perf_counter() - started
        after = await spending()
        deltas = {team: after[team] - before[team] for team in team_keys}
        delta = sum(deltas.values())
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "workload": "local_mock_http",
        "requests": args.requests,
        "concurrency": args.concurrency,
        "statuses": dict(statuses),
        "requests_by_team": dict(requests_by_team),
        "wall_seconds": wall_seconds,
        "completed_requests_per_second": args.requests / wall_seconds,
        "success_latency_ms": {
            "p50": percentile(success_latencies, 0.5),
            "p95": percentile(success_latencies, 0.95),
            "p99": percentile(success_latencies, 0.99),
        },
        "accounting": {
            "response_total_microusd": budget,
            "ledger_delta_microusd": delta,
            "matches": budget == delta,
            "ledger_delta_by_team": deltas,
            "response_cost_by_team": dict(costs_by_team),
        },
        "limitations": [
            "Mock responses do not measure LLM quality or inference performance.",
            "Latency includes the provider, auditing, network, and gateway.",
            "Budget equality assumes no other concurrent workload.",
        ],
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if statuses.get("200", 0) != args.requests or not report["accounting"]["matches"]:
        raise SystemExit(
            "Workload had rejected/failed calls or accounting discrepancy; inspect report"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--output", default="artifacts/load-test.json")
    asyncio.run(benchmark(parser.parse_args()))


if __name__ == "__main__":
    main()
