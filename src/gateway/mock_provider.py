"""A deterministic development server, not an LLM or a quality benchmark."""

import asyncio
import json
import os
from collections import defaultdict

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field

from gateway.schemas import StrictModel


class Scenario(StrictModel):
    model: str = "mock-primary"
    scenario: str = Field(pattern=r"^(normal|unavailable|rate_limit|auth_error|midstream)$")


def create_mock_app() -> FastAPI:
    app = FastAPI(title="Mock provider for gateway verification")
    app.state.scenarios = {}
    app.state.calls = defaultdict(int)

    def admin(authorization):
        expected = os.environ.get("GATEWAY_ADMIN_KEY", "")
        if not expected or authorization != f"Bearer {expected}":
            raise HTTPException(401, "Admin credential required")

    @app.get("/health")
    async def health():
        return {"status": "mock"}

    @app.put("/control/scenario")
    async def change(body: Scenario, authorization: str = Header(default="")):
        admin(authorization)
        app.state.scenarios[body.model] = body.scenario
        return body.model_dump()

    @app.get("/control/status")
    async def status(authorization: str = Header(default="")):
        admin(authorization)
        return {"scenarios": app.state.scenarios, "calls": dict(app.state.calls)}

    @app.post("/v1/chat/completions")
    async def chat(body: dict, x_mock_scenario: str | None = Header(default=None)):
        model = body.get("model", "mock-primary")
        app.state.calls[model] += 1
        mode = x_mock_scenario or app.state.scenarios.get(model, "normal")
        status = {"unavailable": 503, "rate_limit": 429, "auth_error": 401}.get(mode)
        if status:
            return JSONResponse(
                status_code=status, content={"error": {"message": "Simulated failure"}}
            )
        messages = body.get("messages", [])
        question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        words = f"Mock response from {model}: {question}".split()[: body.get("max_tokens", 256)]
        answer = " ".join(words)
        usage = {
            "prompt_tokens": sum(len(m["content"].split()) + 4 for m in messages),
            "completion_tokens": len(words),
        }
        if not body.get("stream"):
            return {
                "model": model,
                "choices": [
                    {"message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}
                ],
                "usage": usage,
            }

        async def chunks():
            for index, word in enumerate(words):
                chunk = {"choices": [{"delta": {"content": word + " "}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
                if mode == "midstream":
                    yield 'data: {"error": {"message": "Simulated interrupted stream"}}\n\n'
                    return
                if index % 8 == 0:
                    await asyncio.sleep(0.001)
            yield f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n"
            yield 'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    return app
