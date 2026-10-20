"""FastAPI entry point. Resources are owned by lifespan, not import-time globals."""

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from opentelemetry.propagate import extract
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis

from gateway.audit import open_audit
from gateway.config import GatewayConfig, load_config
from gateway.errors import GatewayError
from gateway.providers import ProviderAdapter
from gateway.schemas import ChatRequest, TeamPolicy
from gateway.service import GatewayService
from gateway.state import MemoryStore, RedisStore
from gateway.telemetry import Metrics, build_tracer

bearer = HTTPBearer(auto_error=False)


def create_app(
    config_path: str | Path | None = None,
    *,
    config: GatewayConfig | None = None,
    store=None,
    provider_client=None,
    audit_path=None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        cfg = config or load_config(
            config_path or os.environ.get("GATEWAY_CONFIG", "config/demo.yaml")
        )
        admin_key = os.environ.get("GATEWAY_ADMIN_KEY", "")
        if len(admin_key) < 16:
            raise RuntimeError("GATEWAY_ADMIN_KEY must contain at least 16 characters")
        keys = {}
        for name, team in cfg.teams.items():
            key = os.environ.get(team.api_key_env, "")
            if len(key) < 16:
                raise RuntimeError(f"Missing or short team credential in {team.api_key_env}")
            digest = hashlib.sha256(key.encode()).digest()
            if digest in keys.values() or hmac.compare_digest(key, admin_key):
                raise RuntimeError("Team and admin credentials must be distinct and unique")
            keys[name] = digest
        backend = os.environ.get("GATEWAY_STATE_BACKEND", "memory")
        if backend not in {"memory", "redis"}:
            raise RuntimeError("GATEWAY_STATE_BACKEND must be memory or redis")
        metrics_key = os.environ.get("GATEWAY_METRICS_KEY", admin_key)
        if len(metrics_key) < 16:
            raise RuntimeError("GATEWAY_METRICS_KEY must contain at least 16 characters")
        # Register cleanup immediately, including when a later startup step fails.
        async with AsyncExitStack() as resources:
            state = store
            if state is None:
                state = (
                    RedisStore(
                        Redis.from_url(
                            os.environ["REDIS_URL"], socket_timeout=3, socket_connect_timeout=3
                        ),
                        os.environ.get("GATEWAY_NAMESPACE", "llmgw"),
                    )
                    if backend == "redis"
                    else MemoryStore()
                )
                resources.push_async_callback(state.close)
            for team, team_cfg in cfg.teams.items():
                await state.seed_policy(team, team_cfg.policy)
            client = provider_client or httpx.AsyncClient(
                timeout=httpx.Timeout(cfg.provider_timeout_seconds, pool=3),
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
                follow_redirects=False,
                trust_env=False,
            )
            if provider_client is None:
                resources.push_async_callback(client.aclose)
            adapters = {}
            for name, provider in cfg.providers.items():
                key = os.environ.get(provider.api_key_env, "") if provider.api_key_env else None
                if provider.api_key_env and not key:
                    raise RuntimeError(f"Missing provider credential in {provider.api_key_env}")
                adapters[name] = ProviderAdapter(provider, client, key)
            audit = await open_audit(audit_path)
            resources.push_async_callback(audit.close)
            tracer_provider, tracer = build_tracer()
            resources.callback(tracer_provider.shutdown)
            metrics = Metrics()
            app.state.config, app.state.store, app.state.audit = cfg, state, audit
            app.state.keys, app.state.admin_digest = (
                keys,
                hashlib.sha256(admin_key.encode()).digest(),
            )
            app.state.metrics_digest = hashlib.sha256(metrics_key.encode()).digest()
            app.state.metrics = metrics
            app.state.tracer = tracer
            app.state.tracer_provider = tracer_provider
            app.state.service = GatewayService(cfg, state, adapters, audit, metrics, tracer)
            yield

    app = FastAPI(
        title="LLM Gateway",
        version="0.1.0",
        lifespan=lifespan,
        description="Team-scoped quotas and failover across provider-neutral chat routes.",
    )

    @app.middleware("http")
    async def request_identifier(request, call_next):
        request.state.request_id = uuid.uuid4().hex
        with app.state.tracer.start_as_current_span(
            "gateway.accept",
            context=extract(dict(request.headers)),
            attributes={
                "http.request.method": request.method,
                "request.id": request.state.request_id,
            },
        ):
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(request, error):
        headers = {"Retry-After": str(error.retry_after)} if error.retry_after else {}
        return JSONResponse(
            status_code=error.status,
            headers=headers,
            content={
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    @app.exception_handler(Exception)
    async def infrastructure_error(request, error):
        app.state.metrics.logger.error(
            "infrastructure_error", extra={"request_id": request.state.request_id}
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "infrastructure_unavailable",
                    "message": "Gateway infrastructure is unavailable",
                    "request_id": request.state.request_id,
                }
            },
        )

    async def team_auth(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        with app.state.tracer.start_as_current_span("authentication.team"):
            digest = hashlib.sha256(
                (credentials.credentials if credentials else "").encode()
            ).digest()
            for team, expected in app.state.keys.items():
                if credentials and hmac.compare_digest(digest, expected):
                    return team
        raise GatewayError("unauthorized", "Valid team Bearer credential required", 401)

    async def admin_auth(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        digest = hashlib.sha256((credentials.credentials if credentials else "").encode()).digest()
        if not credentials or not hmac.compare_digest(digest, app.state.admin_digest):
            raise GatewayError("admin_unauthorized", "Admin Bearer credential required", 401)

    async def metrics_auth(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        digest = hashlib.sha256((credentials.credentials if credentials else "").encode()).digest()
        if not credentials or not (
            hmac.compare_digest(digest, app.state.metrics_digest)
            or hmac.compare_digest(digest, app.state.admin_digest)
        ):
            raise GatewayError("metrics_unauthorized", "Metrics credential required", 401)

    @app.get("/health/live", tags=["health"])
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready", tags=["health"])
    async def ready():
        try:
            checks = await asyncio.wait_for(
                asyncio.gather(app.state.store.health(), app.state.audit.health()), 3
            )
            if all(checks):
                return {"status": "ready"}
        except Exception:
            pass
        raise GatewayError("not_ready", "Gateway storage is unavailable", 503)

    @app.post("/v1/chat/completions", tags=["chat"])
    async def chat(body: ChatRequest, request: Request, team: Annotated[str, Depends(team_auth)]):
        request_id = request.state.request_id
        events = app.state.service.events(body, team, request_id)
        if not body.stream:
            try:
                result = await anext(events)
            finally:
                await events.aclose()
            usage = result.event.usage
            return {
                "id": request_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.event.text},
                        "finish_reason": result.event.finish_reason,
                    }
                ],
                "usage": (
                    {**usage.model_dump(), "total_tokens": usage.total_tokens} if usage else None
                ),
                "gateway": {
                    "provider": result.provider,
                    "served_model": result.served_model,
                    "accounted_microusd": result.cost,
                    "cost_estimated": result.estimated,
                },
            }

        # Prefetch before sending 200. Authentication/quota/provider failures before the
        # first event can still return a proper HTTP error and safely use a fallback.
        # Keep generator execution (and its tracing/deadline contexts) on ONE task.
        # Starlette consumes StreamingResponse on another task; passing the generator
        # directly would incorrectly transfer context-variable tokens between tasks.
        queue = asyncio.Queue(maxsize=2)
        end = object()

        async def produce():
            try:
                async for event in events:
                    await queue.put(event)
            except GatewayError as error:
                await queue.put(error)
            except asyncio.CancelledError:
                raise
            except Exception:
                await queue.put(
                    GatewayError(
                        "infrastructure_unavailable", "Gateway infrastructure is unavailable", 503
                    )
                )
            finally:
                await events.aclose()
            await queue.put(end)

        producer = asyncio.create_task(produce())
        try:
            first = await queue.get()
        except BaseException:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            raise
        if isinstance(first, GatewayError):
            await producer
            raise first
        if first is end:
            await producer
            raise GatewayError("empty_provider_response", "Provider returned no response", 502)

        async def sse():
            try:
                result = first
                while True:
                    chunk = {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "model": body.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": result.event.text},
                                "finish_reason": result.event.finish_reason
                                if result.event.done
                                else None,
                            }
                        ],
                        "gateway": {
                            "provider": result.provider,
                            "served_model": result.served_model,
                        },
                    }
                    if result.event.done:
                        chunk["usage"] = (
                            result.event.usage.model_dump() if result.event.usage else None
                        )
                        chunk["gateway"].update(
                            accounted_microusd=result.cost, cost_estimated=result.estimated
                        )
                    yield f"data: {json.dumps(chunk)}\n\n"
                    if result.event.done:
                        break
                    result = await queue.get()
                    if isinstance(result, GatewayError):
                        raise result
                    if result is end:
                        raise GatewayError(
                            "stream_interrupted", "Provider stream ended unexpectedly", 502
                        )
            except GatewayError as error:
                payload = {"error": {"code": error.code, "message": error.message}}
                yield f"data: {json.dumps(payload)}\n\n"
            finally:
                if not producer.done():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/metrics", dependencies=[Depends(metrics_auth)], tags=["admin"])
    async def metrics():
        for team in app.state.config.teams:
            policy = await app.state.store.policy(team)
            spent = await app.state.store.spent(team)
            app.state.metrics.budget.labels(team).set(
                spent / policy.daily_budget_microusd
                if policy.daily_budget_microusd
                else float(spent > 0)
            )
        return Response(generate_latest(app.state.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/admin/teams/{team}", dependencies=[Depends(admin_auth)], tags=["admin"])
    async def get_team(team: str):
        if team not in app.state.config.teams:
            raise GatewayError("unknown_team", "Team does not exist", 404)
        return {
            "team": team,
            "policy": (await app.state.store.policy(team)).model_dump(),
            "spent_microusd": await app.state.store.spent(team),
        }

    @app.put("/admin/teams/{team}/policy", dependencies=[Depends(admin_auth)], tags=["admin"])
    async def update_team(team: str, policy: TeamPolicy, request: Request):
        if team not in app.state.config.teams:
            raise GatewayError("unknown_team", "Team does not exist", 404)
        if any(model not in app.state.config.routes for model in policy.allowed_models):
            raise GatewayError(
                "unknown_model", "Policy references an unconfigured model alias", 422
            )
        await app.state.audit.write(
            "policy_update_intent",
            request.state.request_id,
            team,
            actor="admin",
            policy=policy.model_dump(exclude={"system_prompt"}),
        )
        await app.state.store.update_policy(team, policy)
        await app.state.service.audit_event(
            "policy_updated", request.state.request_id, team, actor="admin"
        )
        return {"team": team, "policy": policy.model_dump()}

    @app.get("/admin/providers", dependencies=[Depends(admin_auth)], tags=["admin"])
    async def providers():
        return {
            name: {"circuit": await breaker.state()}
            for name, breaker in app.state.service.breakers.items()
        }

    @app.get("/admin/audit", dependencies=[Depends(admin_auth)], tags=["admin"])
    async def audit():
        return {"events": await app.state.audit.recent()}

    return app
