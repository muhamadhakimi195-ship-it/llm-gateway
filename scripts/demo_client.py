"""Reproducible local scenarios. Fault demonstrations restore the original policy."""

import argparse
import asyncio
import json
import os
from urllib.parse import urlsplit

import httpx


async def run(args):
    team = {"Authorization": "Bearer " + os.environ.get("DEMO_TEAM_KEY", "demo-team-key-change-me")}
    admin = {
        "Authorization": "Bearer " + os.environ.get("GATEWAY_ADMIN_KEY", "demo-admin-key-change-me")
    }
    body = {
        "model": "balanced",
        "messages": [{"role": "user", "content": args.question}],
        "max_tokens": 64,
    }
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        if args.scenario == "stream":
            async with client.stream(
                "POST",
                args.url + "/v1/chat/completions",
                headers=team,
                json={**body, "stream": True},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line:
                        print(line)
            return
        if args.scenario == "ask":
            response = await client.post(args.url + "/v1/chat/completions", headers=team, json=body)
            print(response.status_code, json.dumps(response.json(), indent=2))
            response.raise_for_status()
            return
        # These commands deliberately mutate local demo policy and mock fault state.
        if urlsplit(args.url).hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Fault demonstrations are restricted to loopback URLs")
        current = await client.get(args.url + "/admin/teams/demo", headers=admin)
        current.raise_for_status()
        original = current.json()["policy"]
        try:
            if args.scenario == "failover":
                changed = await client.put(
                    args.mock_url + "/control/scenario",
                    headers=admin,
                    json={"model": "mock-primary", "scenario": "unavailable"},
                )
                changed.raise_for_status()
            else:
                policy = dict(original)
                if args.scenario == "rate-limit":
                    policy["requests_per_minute"] = 1
                else:
                    policy["daily_budget_microusd"] = 0
                changed = await client.put(
                    args.url + "/admin/teams/demo/policy", headers=admin, json=policy
                )
                changed.raise_for_status()
            responses = [
                await client.post(args.url + "/v1/chat/completions", headers=team, json=body)
                for _ in range(3 if args.scenario == "rate-limit" else 1)
            ]
            for response in responses:
                print(response.status_code, json.dumps(response.json(), indent=2))
            if args.scenario == "failover":
                assert responses[0].json()["gateway"]["provider"] == "secondary"
            elif args.scenario == "rate-limit":
                assert any(r.status_code == 429 for r in responses)
            else:
                assert responses[0].status_code == 402
        finally:
            await client.put(args.url + "/admin/teams/demo/policy", headers=admin, json=original)
            if args.scenario == "failover":
                await client.put(
                    args.mock_url + "/control/scenario",
                    headers=admin,
                    json={"model": "mock-primary", "scenario": "normal"},
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=["ask", "stream", "failover", "rate-limit", "budget"])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--mock-url", default="http://127.0.0.1:9001")
    parser.add_argument("--question", default="Why do retries need their own budget reservations?")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
