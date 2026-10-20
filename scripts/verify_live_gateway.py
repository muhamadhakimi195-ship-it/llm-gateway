"""Send small real HTTP requests; never use the mock fault-control endpoints.

Run this against an already running gateway. The report contains our fixed test
question's answers and metadata, but never credentials or database URLs.
"""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


async def verify(args):
    if urlsplit(args.url).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("This local verification client only accepts loopback URLs")
    key = os.environ.get("DEMO_TEAM_KEY", "demo-team-key-change-me")
    headers = {"Authorization": f"Bearer {key}"}
    body = {
        "model": "balanced",
        "messages": [{"role": "user", "content": "What does an API gateway do? One sentence."}],
        "max_tokens": 64,
        "temperature": 0.0,
    }
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        response = await client.post(args.url + "/v1/chat/completions", headers=headers, json=body)
        response.raise_for_status()
        normal = response.json()
        assert normal["choices"][0]["message"]["content"].strip(), "Empty normal answer"
        assert normal["gateway"]["provider"] == args.provider, "Unexpected provider"
        assert normal.get("usage"), "Missing normal token usage"

        text, chunks, done, final = [], 0, False, None
        stream_id = None
        async with client.stream(
            "POST",
            args.url + "/v1/chat/completions",
            headers=headers,
            json={**body, "stream": True},
        ) as stream:
            stream.raise_for_status()
            assert "text/event-stream" in stream.headers["content-type"]
            async for line in stream.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    done = True
                    break
                chunk = json.loads(data)
                assert "error" not in chunk, f"Stream error: {chunk.get('error')}"
                assert chunk["gateway"]["provider"] == args.provider
                stream_id = chunk["id"]
                chunks += 1
                text.append(chunk["choices"][0]["delta"].get("content", ""))
                if chunk["choices"][0]["finish_reason"] is not None:
                    final = chunk
        assert done and final is not None and "".join(text).strip(), "Incomplete stream"
        assert final.get("usage"), "Missing stream token usage"

    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "gateway_url": args.url,
        "normal": normal,
        "stream": {
            "request_id": stream_id,
            "answer": "".join(text),
            "chunks": chunks,
            "done": done,
            "usage": final["usage"],
            "gateway": final["gateway"],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--provider", default="local")
    parser.add_argument("--output", type=Path, default=Path("artifacts/live-verification.json"))
    asyncio.run(verify(parser.parse_args()))


if __name__ == "__main__":
    main()
