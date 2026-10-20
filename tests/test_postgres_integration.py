"""Opt-in real PostgreSQL tests. Only use a dedicated gateway/test database."""

import asyncio
import os
import uuid

import httpx
import pytest
from alembic import command
from alembic.config import Config

from gateway.app import create_app
from gateway.config import load_config
from gateway.mock_provider import create_mock_app
from gateway.postgres_audit import PostgresAuditLog
from gateway.state import MemoryStore

pytestmark = pytest.mark.integration


@pytest.fixture
async def postgres_url(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    return url


async def test_postgres_json_metadata_survives_pool_restart(postgres_url):
    request_id = uuid.uuid4().hex
    first = PostgresAuditLog(postgres_url)
    try:
        assert await first.health()
        await first.write("request", request_id, "demo", status="success", tokens=12)
    finally:
        await first.close()
    second = PostgresAuditLog(postgres_url)
    try:
        record = next(row for row in await second.recent() if row["request_id"] == request_id)
        assert record["payload"] == {"status": "success", "tokens": 12}
        assert record["timestamp"] and record["team"] == "demo"
    finally:
        await second.close()


async def test_postgres_concurrent_writes_are_committed(postgres_url):
    audit = PostgresAuditLog(postgres_url)
    ids = {uuid.uuid4().hex for _ in range(16)}
    try:
        await asyncio.gather(
            *(audit.write("request", rid, "demo", status="success") for rid in ids)
        )
        assert ids <= {row["request_id"] for row in await audit.recent()}
    finally:
        await audit.close()


async def test_postgres_api_audit_survives_application_restart(postgres_url, monkeypatch):
    monkeypatch.setenv("GATEWAY_AUDIT_BACKEND", "postgres")
    monkeypatch.setenv("GATEWAY_ADMIN_KEY", "integration-admin-key-at-least-16")
    monkeypatch.setenv("DEMO_TEAM_KEY", "integration-team-key-at-least-16")
    monkeypatch.setenv("ANALYST_TEAM_KEY", "integration-analyst-key-at-least-16")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    config = load_config("config/demo.yaml")
    body = {"model": "balanced", "messages": [{"role": "user", "content": "Private test text"}]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_mock_app()), base_url="http://mock"
    ) as upstream:
        app = create_app(config=config, store=MemoryStore(), provider_client=upstream)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                response = await client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer integration-team-key-at-least-16"},
                )
                assert response.status_code == 200
                request_id = response.json()["id"]
        restarted = create_app(config=config, store=MemoryStore(), provider_client=upstream)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted), base_url="http://gateway"
            ) as client:
                result = await client.get(
                    "/admin/audit",
                    headers={"Authorization": "Bearer integration-admin-key-at-least-16"},
                )
                assert result.status_code == 200
                records = [r for r in result.json()["events"] if r["request_id"] == request_id]
                assert {r["kind"] for r in records} == {"request", "attempt"}
                assert "Private test text" not in result.text
