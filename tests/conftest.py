from dataclasses import dataclass

import httpx
import pytest

from gateway.app import create_app
from gateway.config import load_config
from gateway.mock_provider import create_mock_app
from gateway.runtime import loop_factory
from gateway.state import MemoryStore


@pytest.hookimpl(optionalhook=True)
def pytest_asyncio_loop_factories(config, item):
    # pytest-asyncio 1.4+ uses the same explicit factory as the real Windows server.
    return {"gateway": loop_factory}


@dataclass
class Harness:
    client: httpx.AsyncClient
    app: object
    mock: object
    store: MemoryStore


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_ADMIN_KEY", "test-admin-key-at-least-16")
    monkeypatch.setenv("DEMO_TEAM_KEY", "test-team-key-at-least-16")
    monkeypatch.setenv("ANALYST_TEAM_KEY", "test-analyst-key-at-least-16")
    monkeypatch.setenv("GATEWAY_METRICS_KEY", "test-metrics-key-at-least-16")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    cfg = load_config("config/demo.yaml")
    cfg.retry_base_seconds = 0.0
    cfg.circuit_failure_threshold = 2
    mock = create_mock_app()
    store = MemoryStore()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock), base_url="http://mock"
    ) as upstream:
        app = create_app(
            config=cfg, store=store, provider_client=upstream, audit_path=tmp_path / "audit.db"
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                yield Harness(client, app, mock, store)


@pytest.fixture
def chat_body():
    return {
        "model": "balanced",
        "messages": [{"role": "user", "content": "Explain a circuit breaker."}],
        "max_tokens": 32,
    }


@pytest.fixture
def team_headers():
    return {"Authorization": "Bearer test-team-key-at-least-16"}


@pytest.fixture
def admin_headers():
    return {"Authorization": "Bearer test-admin-key-at-least-16"}
