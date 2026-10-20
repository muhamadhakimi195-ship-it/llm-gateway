import json

import httpx
import pytest

from gateway.config import ProviderConfig, load_config
from gateway.errors import ProviderError
from gateway.providers import ProviderAdapter
from gateway.schemas import ChatRequest, Usage
from gateway.service import cost_microusd


def request():
    return ChatRequest(model="balanced", messages=[{"role": "user", "content": "Hello"}])


@pytest.mark.parametrize(
    "kind,payload",
    [
        (
            "compatible",
            {
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        ),
        ("ollama", {"message": {"content": "hello"}, "prompt_eval_count": 2, "eval_count": 1}),
        (
            "anthropic",
            {
                "content": [{"type": "text", "text": "hello"}],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        ),
    ],
)
async def test_three_protocols_normalize_reply(kind, payload):
    async def handler(incoming):
        body = json.loads(incoming.content)
        assert body["model"] == "physical-model"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        config = ProviderConfig(
            kind=kind, base_url="https://provider.example", model="physical-model"
        )
        result = await ProviderAdapter(config, client, "secret").generate(request())
    assert result.content == "hello"
    assert result.usage.total_tokens == 3


@pytest.mark.parametrize("status,retryable", [(401, False), (400, False), (429, True), (503, True)])
async def test_status_mapping_never_exposes_provider_body(status, retryable):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda incoming: httpx.Response(status, text="private-provider-detail")
        )
    ) as client:
        adapter = ProviderAdapter(
            ProviderConfig(kind="compatible", base_url="https://provider.example", model="m"),
            client,
        )
        with pytest.raises(ProviderError) as caught:
            await adapter.generate(request())
    assert caught.value.retryable == retryable
    assert "private-provider-detail" not in str(caught.value)


async def test_malformed_response_is_retryable_and_charge_uncertain():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda incoming: httpx.Response(200, json={}))
    ) as client:
        adapter = ProviderAdapter(
            ProviderConfig(kind="compatible", base_url="https://provider.example", model="m"),
            client,
        )
        with pytest.raises(ProviderError) as caught:
            await adapter.generate(request())
    assert caught.value.retryable and caught.value.uncertain_charge


@pytest.mark.parametrize(
    "kind,payload",
    [
        (
            "ollama",
            '{"message":{"content":"hi"},"done":false}\n{"message":{"content":""},"done":true,"prompt_eval_count":2,"eval_count":1}\n',
        ),
        (
            "anthropic",
            'data: {"type":"message_start","message":{"usage":{"input_tokens":2}}}\n\n'
            'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
            'data: {"type":"message_delta","usage":{"output_tokens":1}}\n\n'
            'data: {"type":"message_stop"}\n\n',
        ),
    ],
)
async def test_other_protocols_stream_usage(kind, payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda incoming: httpx.Response(200, text=payload))
    ) as client:
        adapter = ProviderAdapter(
            ProviderConfig(kind=kind, base_url="https://provider.example", model="m"), client
        )
        events = [event async for event in adapter.stream(request())]
    assert "".join(event.text for event in events) == "hi"
    assert events[-1].done and events[-1].usage.total_tokens == 3


def test_cost_rounds_up_using_decimal_arithmetic():
    cfg = ProviderConfig(
        kind="compatible",
        base_url="https://provider.example",
        model="m",
        input_usd_per_million=0.1,
        output_usd_per_million=0.2,
    )
    assert cost_microusd(cfg, Usage(prompt_tokens=1, completion_tokens=1)) == 1


def test_all_shipped_configs_are_structurally_valid():
    for path in ["config/demo.yaml", "config/ollama.yaml", "config/providers.example.yaml"]:
        load_config(path)
