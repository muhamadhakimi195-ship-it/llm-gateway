"""Optional real local-inference check using the configured Ollama model.

This starts no public server and uses no hosted API. It still performs model
inference, so run it only when the local Ollama service and model are available.
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from gateway.app import create_app


async def main():
    os.environ.setdefault("GATEWAY_ADMIN_KEY", "demo-admin-key-change-me")
    os.environ.setdefault("DEMO_TEAM_KEY", "demo-team-key-change-me")
    app = create_app("config/ollama.yaml", audit_path="artifacts/ollama-audit.sqlite3")
    body = {
        "model": "balanced",
        "max_tokens": 32,
        "messages": [
            {"role": "user", "content": "Explain what an API gateway does in one sentence."}
        ],
    }
    headers = {"Authorization": "Bearer " + os.environ["DEMO_TEAM_KEY"]}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway", timeout=120
        ) as client:
            normal = await client.post("/v1/chat/completions", json=body, headers=headers)
            normal.raise_for_status()
            assert normal.json()["choices"][0]["message"]["content"].strip()
            streamed = await client.post(
                "/v1/chat/completions", json={**body, "stream": True}, headers=headers
            )
            streamed.raise_for_status()
            assert streamed.text.endswith("data: [DONE]\n\n")
            assert '"error"' not in streamed.text
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "normal_status": normal.status_code,
        "stream_status": streamed.status_code,
        "normal_response": normal.json(),
        "stream_completed": True,
        "provider": "local_ollama",
    }
    path = Path("artifacts/ollama-verification.json")
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
