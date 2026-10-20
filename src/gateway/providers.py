"""Translate a small gateway contract into three provider protocols.

No vendor SDK is required: HTTPX supplies connection pooling, bounded timeouts,
and streaming. Provider adapters can be tested against an in-process HTTP transport.
"""

import json
from collections.abc import AsyncIterator

import httpx
from pydantic import ValidationError

from gateway.config import ProviderConfig
from gateway.errors import ProviderError
from gateway.schemas import ChatRequest, ProviderReply, StreamEvent, Usage


def check_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    status = response.status_code
    if status == 429:
        raise ProviderError("provider_rate_limit", True)
    if status in {401, 403}:
        raise ProviderError("provider_authentication", False)
    if status >= 500 or status in {408, 425}:
        raise ProviderError("provider_unavailable", True, uncertain_charge=True)
    raise ProviderError("provider_rejected_request", False)


def usage_from(data: dict) -> Usage | None:
    raw = data.get("usage")
    if raw is None:
        return None
    return Usage(prompt_tokens=raw["prompt_tokens"], completion_tokens=raw["completion_tokens"])


def finish_reason(value: str | None) -> str:
    return "length" if value in {"length", "max_tokens"} else "stop"


class ProviderAdapter:
    def __init__(
        self, config: ProviderConfig, client: httpx.AsyncClient, api_key: str | None = None
    ):
        self.config, self.client, self.api_key = config, client, api_key
        self.base_url = config.resolved_url()

    def request_parts(self, request: ChatRequest, stream: bool) -> tuple[str, dict, dict]:
        cfg = self.config
        headers = {}
        if cfg.mock_scenario:
            headers["x-mock-scenario"] = cfg.mock_scenario
        messages = [m.model_dump() for m in request.messages]
        if cfg.kind == "compatible":
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
            body = dict(
                model=cfg.model,
                messages=messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                stream=stream,
            )
            if stream:
                body["stream_options"] = {"include_usage": True}
            return self.base_url + "/chat/completions", headers, body
        if cfg.kind == "ollama":
            body = dict(
                model=cfg.model,
                messages=messages,
                stream=stream,
                options={"temperature": request.temperature, "num_predict": request.max_tokens},
            )
            return self.base_url + "/api/chat", headers, body
        if request.temperature > 1:
            raise ProviderError("anthropic_temperature_out_of_range", False)
        headers.update({"anthropic-version": "2023-06-01", "x-api-key": self.api_key or ""})
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        body = dict(
            model=cfg.model,
            messages=[m for m in messages if m["role"] != "system"],
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            stream=stream,
        )
        if system:
            body["system"] = system
        return self.base_url + "/v1/messages", headers, body

    async def generate(self, request: ChatRequest) -> ProviderReply:
        url, headers, body = self.request_parts(request, False)
        try:
            response = await self.client.post(url, headers=headers, json=body)
            check_status(response)
            data = response.json()
            if self.config.kind == "compatible":
                choice = data["choices"][0]
                reply = ProviderReply(
                    content=choice["message"]["content"],
                    usage=usage_from(data),
                    finish_reason=finish_reason(choice.get("finish_reason")),
                )
            elif self.config.kind == "ollama":
                usage = None
                if "prompt_eval_count" in data and "eval_count" in data:
                    usage = Usage(
                        prompt_tokens=data["prompt_eval_count"],
                        completion_tokens=data["eval_count"],
                    )
                reply = ProviderReply(
                    content=data["message"]["content"],
                    usage=usage,
                    finish_reason=finish_reason(data.get("done_reason")),
                )
            else:
                usage = data.get("usage")
                reply = ProviderReply(
                    content="".join(
                        block["text"] for block in data["content"] if block["type"] == "text"
                    ),
                    usage=Usage(
                        prompt_tokens=usage["input_tokens"],
                        completion_tokens=usage["output_tokens"],
                    )
                    if usage
                    else None,
                    finish_reason=finish_reason(data.get("stop_reason")),
                )
            if not reply.content or len(reply.content.encode("utf-8")) > 1_000_000:
                raise ProviderError("invalid_provider_response", True, uncertain_charge=True)
            return reply
        except httpx.HTTPError as error:
            raise ProviderError("provider_transport_error", True, uncertain_charge=True) from error
        except (ValueError, KeyError, IndexError, TypeError, ValidationError) as error:
            raise ProviderError("invalid_provider_response", True, uncertain_charge=True) from error

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        url, headers, body = self.request_parts(request, True)
        try:
            async with self.client.stream("POST", url, headers=headers, json=body) as response:
                check_status(response)
                if self.config.kind == "ollama":
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        data = json.loads(line)
                        if data.get("error"):
                            raise ProviderError(
                                "provider_stream_error", True, uncertain_charge=True
                            )
                        usage = None
                        if "prompt_eval_count" in data and "eval_count" in data:
                            usage = Usage(
                                prompt_tokens=data["prompt_eval_count"],
                                completion_tokens=data["eval_count"],
                            )
                        event = StreamEvent(
                            text=data.get("message", {}).get("content", ""),
                            usage=usage,
                            done=data.get("done", False),
                            finish_reason=finish_reason(data.get("done_reason")),
                        )
                        yield event
                        if event.done:
                            return
                else:
                    usage = None
                    prompt_tokens, completion_tokens = 0, 0
                    saw_finish = False
                    reason = "stop"
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            yield StreamEvent(usage=usage, done=True, finish_reason=reason)
                            return
                        data = json.loads(payload)
                        if self.config.kind == "compatible":
                            if data.get("error"):
                                raise ProviderError(
                                    "provider_stream_error", True, uncertain_charge=True
                                )
                            event_usage = usage_from(data)
                            if event_usage:
                                usage = event_usage
                                yield StreamEvent(usage=usage)
                            for choice in data.get("choices", []):
                                if choice.get("finish_reason"):
                                    saw_finish = True
                                    reason = finish_reason(choice["finish_reason"])
                                text = choice.get("delta", {}).get("content")
                                if text:
                                    yield StreamEvent(text=text)
                        else:
                            kind = data.get("type")
                            if kind == "error":
                                raise ProviderError(
                                    "provider_stream_error", True, uncertain_charge=True
                                )
                            if kind == "message_start":
                                prompt_tokens = data["message"]["usage"]["input_tokens"]
                            elif kind == "content_block_delta":
                                text = data.get("delta", {}).get("text", "")
                                if text:
                                    yield StreamEvent(text=text)
                            elif kind == "message_delta":
                                completion_tokens = data["usage"]["output_tokens"]
                                reason = finish_reason(data.get("delta", {}).get("stop_reason"))
                            elif kind == "message_stop":
                                yield StreamEvent(
                                    usage=Usage(
                                        prompt_tokens=prompt_tokens,
                                        completion_tokens=completion_tokens,
                                    ),
                                    done=True,
                                    finish_reason=reason,
                                )
                                return
                    if saw_finish:
                        yield StreamEvent(usage=usage, done=True, finish_reason=reason)
                        return
                raise ProviderError("incomplete_provider_stream", True, uncertain_charge=True)
        except httpx.HTTPError as error:
            raise ProviderError("provider_transport_error", True, uncertain_charge=True) from error
        except (ValueError, KeyError, IndexError, TypeError, ValidationError) as error:
            raise ProviderError("invalid_provider_stream", True, uncertain_charge=True) from error
