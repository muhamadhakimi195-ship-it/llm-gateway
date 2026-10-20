import asyncio
import json

from gateway.schemas import ChatRequest


async def test_authentication_and_admin_isolation(harness, chat_body, team_headers):
    assert (await harness.client.post("/v1/chat/completions", json=chat_body)).status_code == 401
    assert (await harness.client.get("/admin/teams/demo", headers=team_headers)).status_code == 401
    assert harness.mock.state.calls == {}


async def test_normal_reply_has_usage_accounting_and_correlation(harness, chat_body, team_headers):
    response = await harness.client.post(
        "/v1/chat/completions", json=chat_body, headers=team_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["gateway"]["provider"] == "primary"
    assert not body["gateway"]["cost_estimated"]
    assert body["usage"]["total_tokens"] > 0
    assert body["id"] == response.headers["X-Request-ID"]
    assert await harness.store.spent("demo") == body["gateway"]["accounted_microusd"]


async def test_primary_outage_retries_then_falls_back(harness, chat_body, team_headers):
    harness.mock.state.scenarios["mock-primary"] = "unavailable"
    response = await harness.client.post(
        "/v1/chat/completions", json=chat_body, headers=team_headers
    )
    assert response.status_code == 200
    assert response.json()["gateway"]["provider"] == "secondary"
    assert response.json()["gateway"]["cost_estimated"]
    assert harness.mock.state.calls["mock-primary"] == 2
    assert harness.mock.state.calls["mock-secondary"] == 1
    assert await harness.app.state.service.breakers["primary"].state() == "open"
    # The response's request cost includes uncertain failed attempts, not just the winner.
    assert await harness.store.spent("demo") == response.json()["gateway"]["accounted_microusd"]
    response2 = await harness.client.post(
        "/v1/chat/completions", json=chat_body, headers=team_headers
    )
    assert response2.status_code == 200
    assert harness.mock.state.calls["mock-primary"] == 2


async def test_auth_error_is_terminal_and_never_falls_back(harness, chat_body, team_headers):
    harness.mock.state.scenarios["mock-primary"] = "auth_error"
    response = await harness.client.post(
        "/v1/chat/completions", json=chat_body, headers=team_headers
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_authentication"
    assert harness.mock.state.calls["mock-primary"] == 1
    assert harness.mock.state.calls["mock-secondary"] == 0
    assert await harness.store.spent("demo") == 0


async def test_budget_blocks_before_provider_call(harness, chat_body, team_headers):
    policy = await harness.store.policy("demo")
    await harness.store.update_policy(
        "demo", policy.model_copy(update={"daily_budget_microusd": 1})
    )
    response = await harness.client.post(
        "/v1/chat/completions", json=chat_body, headers=team_headers
    )
    assert response.status_code == 402
    assert harness.mock.state.calls == {}
    assert await harness.store.spent("demo") == 0


async def test_rate_limit_is_atomic_under_api_concurrency(harness, chat_body, team_headers):
    policy = await harness.store.policy("demo")
    await harness.store.update_policy("demo", policy.model_copy(update={"requests_per_minute": 3}))
    responses = await asyncio.gather(
        *(
            harness.client.post("/v1/chat/completions", json=chat_body, headers=team_headers)
            for _ in range(20)
        )
    )
    assert sum(r.status_code == 200 for r in responses) == 3
    rejected = [r for r in responses if r.status_code == 429]
    assert len(rejected) == 17
    assert all(int(r.headers["Retry-After"]) >= 1 for r in rejected)


async def test_admin_policy_update_and_metadata_only_audit(
    harness, admin_headers, team_headers, chat_body
):
    policy = await harness.store.policy("demo")
    policy.requests_per_minute = 7
    response = await harness.client.put(
        "/admin/teams/demo/policy", json=policy.model_dump(), headers=admin_headers
    )
    assert response.status_code == 200
    assert (await harness.store.policy("demo")).requests_per_minute == 7
    await harness.client.post("/v1/chat/completions", json=chat_body, headers=team_headers)
    audit = await harness.client.get("/admin/audit", headers=admin_headers)
    assert "policy_updated" in audit.text
    assert chat_body["messages"][0]["content"] not in audit.text
    assert team_headers["Authorization"] not in audit.text


async def test_forbidden_model_and_invalid_input_never_call_provider(
    harness, chat_body, team_headers
):
    chat_body["model"] = "not-allowed"
    assert (
        await harness.client.post("/v1/chat/completions", json=chat_body, headers=team_headers)
    ).status_code == 403
    chat_body["messages"][0]["content"] = "   "
    assert (
        await harness.client.post("/v1/chat/completions", json=chat_body, headers=team_headers)
    ).status_code == 422
    assert harness.mock.state.calls == {}


async def test_stream_has_usage_and_done_marker(harness, chat_body, team_headers):
    chat_body["stream"] = True
    response = await harness.client.post(
        "/v1/chat/completions", json=chat_body, headers=team_headers
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["Content-Type"]
    assert response.text.endswith("data: [DONE]\n\n")
    chunks = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and "[DONE]" not in line
    ]
    assert any(chunk.get("usage") for chunk in chunks)
    assert {c["gateway"]["provider"] for c in chunks} == {"primary"}


async def test_stream_falls_back_only_before_output(harness, chat_body, team_headers):
    chat_body["stream"] = True
    harness.mock.state.scenarios["mock-primary"] = "unavailable"
    response = await harness.client.post(
        "/v1/chat/completions", json=chat_body, headers=team_headers
    )
    assert response.status_code == 200
    assert '"provider": "secondary"' in response.text
    assert '"provider": "primary"' not in response.text


async def test_midstream_failure_never_appends_fallback(harness, chat_body, team_headers):
    chat_body["stream"] = True
    harness.mock.state.scenarios["mock-primary"] = "midstream"
    response = await harness.client.post(
        "/v1/chat/completions", json=chat_body, headers=team_headers
    )
    assert response.status_code == 200
    assert "stream_interrupted" in response.text
    assert harness.mock.state.calls["mock-primary"] == 1
    assert harness.mock.state.calls["mock-secondary"] == 0
    assert await harness.store.spent("demo") > 0


async def test_abandoned_stream_retains_conservative_charge(harness, chat_body):
    request = ChatRequest.model_validate({**chat_body, "stream": True})
    events = harness.app.state.service.events(request, "demo", "cancel-test")
    first = await anext(events)
    assert first.event.text
    reserved = await harness.store.spent("demo")
    await events.aclose()
    assert await harness.store.spent("demo") == reserved
    assert reserved > 0


async def test_deadline_is_bounded(harness, chat_body, team_headers):
    service = harness.app.state.service
    service.config.request_deadline_seconds = 0.01

    async def slow_generate(request):
        await asyncio.sleep(0.1)

    service.adapters["primary"].generate = slow_generate
    response = await harness.client.post(
        "/v1/chat/completions", json=chat_body, headers=team_headers
    )
    assert response.status_code == 504
    assert await harness.store.spent("demo") > 0


async def test_metrics_requires_admin_and_contains_real_accounting(
    harness, chat_body, team_headers, admin_headers
):
    await harness.client.post("/v1/chat/completions", json=chat_body, headers=team_headers)
    assert (await harness.client.get("/metrics")).status_code == 401
    response = await harness.client.get("/metrics", headers=admin_headers)
    assert response.status_code == 200
    assert 'gateway_requests_total{status="success",team="demo"} 1.0' in response.text
    assert "gateway_accounted_microusd_total" in response.text


async def test_no_usage_is_marked_estimated(harness, chat_body, team_headers):
    adapter = harness.app.state.service.adapters["primary"]

    async def no_usage(request):
        from gateway.schemas import ProviderReply

        return ProviderReply(content="A reply without usage metadata")

    adapter.generate = no_usage
    response = await harness.client.post(
        "/v1/chat/completions", json=chat_body, headers=team_headers
    )
    assert response.status_code == 200
    assert response.json()["usage"] is None
    assert response.json()["gateway"]["cost_estimated"]


async def test_stream_budget_error_is_an_http_error_before_any_events(
    harness, chat_body, team_headers
):
    policy = await harness.store.policy("demo")
    await harness.store.update_policy(
        "demo", policy.model_copy(update={"daily_budget_microusd": 0})
    )
    response = await harness.client.post(
        "/v1/chat/completions", headers=team_headers, json={**chat_body, "stream": True}
    )
    assert response.status_code == 402
    assert "text/event-stream" not in response.headers["Content-Type"]


async def test_batch_work_cannot_occupy_interactive_pool(harness, chat_body, team_headers):
    from gateway.schemas import ProviderReply, Usage

    entered, release = asyncio.Event(), asyncio.Event()

    async def controlled_generate(request):
        if request.priority == "batch":
            entered.set()
            await release.wait()
        return ProviderReply(content="Reply", usage=Usage(prompt_tokens=1, completion_tokens=1))

    service = harness.app.state.service
    service.slots["batch"] = asyncio.Semaphore(1)
    service.slots["interactive"] = asyncio.Semaphore(1)
    service.adapters["primary"].generate = controlled_generate
    batch = asyncio.create_task(
        harness.client.post(
            "/v1/chat/completions", headers=team_headers, json={**chat_body, "priority": "batch"}
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    interactive = await harness.client.post(
        "/v1/chat/completions", headers=team_headers, json=chat_body
    )
    assert interactive.status_code == 200 and not batch.done()
    release.set()
    assert (await batch).status_code == 200


async def test_team_budget_and_rate_limits_are_isolated(harness, chat_body, team_headers):
    other_headers = {"Authorization": "Bearer test-analyst-key-at-least-16"}
    policy = await harness.store.policy("analyst")
    await harness.store.update_policy(
        "analyst", policy.model_copy(update={"daily_budget_microusd": 0})
    )
    assert (
        await harness.client.post("/v1/chat/completions", headers=other_headers, json=chat_body)
    ).status_code == 402
    assert (
        await harness.client.post("/v1/chat/completions", headers=team_headers, json=chat_body)
    ).status_code == 200
    assert await harness.store.spent("analyst") == 0
    assert await harness.store.spent("demo") > 0


async def test_traces_have_metadata_and_do_not_capture_prompts(harness, chat_body, team_headers):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    harness.app.state.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    response = await harness.client.post(
        "/v1/chat/completions", json=chat_body, headers=team_headers
    )
    assert response.status_code == 200
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} >= {
        "gateway.accept",
        "authentication.team",
        "quota.admit",
        "provider.call",
    }
    assert chat_body["messages"][0]["content"] not in str([s.attributes for s in spans])


async def test_metrics_credential_cannot_change_team_policy(harness):
    headers = {"Authorization": "Bearer test-metrics-key-at-least-16"}
    assert (await harness.client.get("/metrics", headers=headers)).status_code == 200
    assert (await harness.client.get("/admin/teams/demo", headers=headers)).status_code == 401


async def test_failed_control_plane_does_not_fall_back_to_memory(harness, chat_body, team_headers):
    async def unavailable(team):
        raise ConnectionError("Control plane unavailable")

    harness.store.policy = unavailable
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app, raise_app_exceptions=False),
        base_url="http://gateway",
    ) as client:
        response = await client.post("/v1/chat/completions", json=chat_body, headers=team_headers)
    assert response.status_code == 503
    assert harness.mock.state.calls == {}
