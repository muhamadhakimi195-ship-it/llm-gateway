"""Request orchestration: admission, bounded retries, failover, and settlement."""

import asyncio
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from pydantic import ValidationError

from gateway.audit import AuditStore
from gateway.circuit import CircuitBreaker
from gateway.config import GatewayConfig, ProviderConfig
from gateway.errors import GatewayError, ProviderError
from gateway.providers import ProviderAdapter
from gateway.schemas import ChatRequest, Message, StreamEvent, Usage
from gateway.state import StateStore
from gateway.telemetry import Metrics


def token_bound(request: ChatRequest) -> int:
    """Conservative planning estimate, not a tokenizer or measured token usage."""
    return 64 + sum(len(m.content.encode("utf-8")) + 32 for m in request.messages)


def cost_microusd(config: ProviderConfig, usage: Usage) -> int:
    # USD per million tokens equals micro-USD per token. Round upward once, using
    # decimal arithmetic, so fractional prices never undercount small requests.
    value = (
        Decimal(str(config.input_usd_per_million)) * usage.prompt_tokens
        + Decimal(str(config.output_usd_per_million)) * usage.completion_tokens
    )
    return int(value.to_integral_value(rounding=ROUND_CEILING))


@dataclass
class GatewayEvent:
    provider: str
    served_model: str
    event: StreamEvent
    cost: int = 0
    estimated: bool = False


class GatewayService:
    def __init__(
        self,
        config: GatewayConfig,
        store: StateStore,
        adapters: dict[str, ProviderAdapter],
        audit: AuditStore,
        metrics: Metrics,
        tracer,
    ):
        self.config, self.store, self.adapters = config, store, adapters
        self.audit, self.metrics, self.tracer = audit, metrics, tracer
        self.breakers = {
            name: CircuitBreaker(
                config.circuit_failure_threshold,
                config.circuit_window_seconds,
                config.circuit_cooldown_seconds,
            )
            for name in config.providers
        }
        # Separate pools prevent batch jobs from occupying interactive capacity.
        # They are bounded per worker, not a cross-replica priority queue.
        self.slots = {
            "interactive": asyncio.Semaphore(config.interactive_concurrency),
            "batch": asyncio.Semaphore(config.batch_concurrency),
        }

    async def audit_event(self, kind, request_id, team, **payload):
        try:
            await self.audit.write(kind, request_id, team, **payload)
        except Exception:
            # An audit-storage failure must not discard an already paid, valid reply.
            self.metrics.logger.exception("audit_write_failed", extra={"request_id": request_id})

    async def settle(
        self, reservation, cfg, usage, estimated, request_id, elapsed, outcome, provider
    ):
        amount = reservation.amount if estimated else (cost_microusd(cfg, usage) if usage else 0)
        applied = await self.store.settle(reservation, amount)
        if not applied:
            return amount
        self.metrics.cost.labels(reservation.team, str(estimated).lower()).inc(amount)
        if usage is not None:
            self.metrics.tokens.labels(reservation.team, "input").inc(usage.prompt_tokens)
            self.metrics.tokens.labels(reservation.team, "output").inc(usage.completion_tokens)
        await self.audit_event(
            "attempt",
            request_id,
            reservation.team,
            outcome=outcome,
            provider=provider,
            served_model=cfg.model,
            accounted_microusd=amount,
            estimated=estimated,
            usage=usage.model_dump() if usage else None,
            elapsed_seconds=elapsed,
        )
        return amount

    async def events(
        self, request: ChatRequest, team: str, request_id: str
    ) -> AsyncIterator[GatewayEvent]:
        started = time.monotonic()
        status = "internal_error"
        slot = self.slots[request.priority]
        acquired = False
        request_cost = 0
        request_estimated = False
        try:
            async with asyncio.timeout(self.config.request_deadline_seconds):
                policy = await self.store.policy(team)
                if request.model not in policy.allowed_models:
                    raise GatewayError(
                        "model_forbidden", "Team is not allowed to use this model alias", 403
                    )
                if policy.system_prompt:
                    try:
                        request = ChatRequest.model_validate(
                            {
                                **request.model_dump(),
                                "messages": [
                                    Message(
                                        role="system", content=policy.system_prompt
                                    ).model_dump(),
                                    *[m.model_dump() for m in request.messages],
                                ],
                            }
                        )
                    except ValidationError as error:
                        raise GatewayError(
                            "enriched_prompt_too_large",
                            "Prompt exceeds limits after team policy enrichment",
                            422,
                        ) from error
                bound = token_bound(request)
                with self.tracer.start_as_current_span("quota.admit", attributes={"team": team}):
                    await self.store.admit(team, policy, bound + request.max_tokens)
                try:
                    await asyncio.wait_for(slot.acquire(), self.config.queue_timeout_seconds)
                    acquired = True
                except TimeoutError as error:
                    raise GatewayError(
                        "gateway_busy", "Priority capacity is full", 503, 1
                    ) from error

                for index, provider in enumerate(self.config.routes[request.model]):
                    breaker = self.breakers[provider]
                    circuit_lease = await breaker.enter()
                    if circuit_lease is None:
                        continue
                    cfg = self.config.providers[provider]
                    try:
                        for attempt in range(self.config.retry_count + 1):
                            reservation = await self.store.reserve(
                                team,
                                policy,
                                cost_microusd(
                                    cfg,
                                    Usage(
                                        prompt_tokens=bound, completion_tokens=request.max_tokens
                                    ),
                                ),
                            )
                            call_started = time.monotonic()
                            emitted = False
                            usage = None
                            settled = False
                            stream = None
                            try:
                                with self.tracer.start_as_current_span(
                                    "provider.call",
                                    attributes={
                                        "team": team,
                                        "provider": provider,
                                        "model.requested": request.model,
                                        "model.served": cfg.model,
                                        "attempt": attempt,
                                    },
                                ):
                                    if request.stream:
                                        stream = self.adapters[provider].stream(request)
                                    else:
                                        reply = await self.adapters[provider].generate(request)

                                        async def one_event(reply=reply):
                                            yield StreamEvent(
                                                text=reply.content,
                                                usage=reply.usage,
                                                done=True,
                                                finish_reason=reply.finish_reason,
                                            )

                                        stream = one_event()
                                    async for event in stream:
                                        usage = event.usage or usage
                                        if event.done:
                                            if not emitted and not event.text:
                                                raise ProviderError(
                                                    "empty_provider_response", True, True
                                                )
                                            estimated = usage is None
                                            amount = await self.settle(
                                                reservation,
                                                cfg,
                                                usage,
                                                estimated,
                                                request_id,
                                                time.monotonic() - call_started,
                                                "success",
                                                provider,
                                            )
                                            request_cost += amount
                                            request_estimated = request_estimated or estimated
                                            settled = True
                                            await breaker.success(circuit_lease)
                                            self.metrics.attempts.labels(provider, "success").inc()
                                            if index:
                                                self.metrics.fallback.labels(request.model).inc()
                                            status = "success"
                                            yield GatewayEvent(
                                                provider,
                                                cfg.model,
                                                event,
                                                request_cost,
                                                request_estimated,
                                            )
                                            return
                                        if event.text:
                                            emitted = True
                                            yield GatewayEvent(provider, cfg.model, event)
                            except ProviderError as error:
                                elapsed = time.monotonic() - call_started
                                # A timeout or interrupted stream may already be billable. Retain
                                # the full bound instead of pretending every failed call is free.
                                request_cost += await self.settle(
                                    reservation,
                                    cfg,
                                    None,
                                    error.uncertain_charge,
                                    request_id,
                                    elapsed,
                                    error.code,
                                    provider,
                                )
                                settled = True
                                request_estimated = request_estimated or error.uncertain_charge
                                self.metrics.attempts.labels(provider, error.code).inc()
                                if error.retryable:
                                    await breaker.failure(circuit_lease)
                                else:
                                    await breaker.abandon(circuit_lease)
                                if emitted:
                                    raise GatewayError(
                                        "stream_interrupted",
                                        "Provider stream ended unexpectedly",
                                        502,
                                    ) from error
                                if not error.retryable:
                                    raise GatewayError(
                                        error.code,
                                        "Provider rejected this request; retry is disabled",
                                        502,
                                    ) from error
                                if (
                                    attempt < self.config.retry_count
                                    and await breaker.state() == "closed"
                                ):
                                    delay = self.config.retry_base_seconds * (2**attempt)
                                    await asyncio.sleep(delay * random.uniform(0.75, 1.25))
                                    continue
                                break
                            finally:
                                self.metrics.provider_latency.labels(provider).observe(
                                    time.monotonic() - call_started
                                )
                                if stream is not None:
                                    await stream.aclose()
                                if not settled:
                                    # Shield accounting from cancellation. Leaving the pre-debit
                                    # in place is also conservative if the entire worker is killed.
                                    await asyncio.shield(
                                        self.settle(
                                            reservation,
                                            cfg,
                                            None,
                                            True,
                                            request_id,
                                            time.monotonic() - call_started,
                                            "abandoned",
                                            provider,
                                        )
                                    )
                    finally:
                        await breaker.abandon(circuit_lease)
                        state = await breaker.state()
                        self.metrics.circuit.labels(provider).set(state != "closed")
                raise GatewayError(
                    "providers_unavailable", "No permitted provider completed the request", 503, 1
                )
        except TimeoutError as error:
            status = "deadline_exceeded"
            raise GatewayError(status, "Gateway request deadline exceeded", 504) from error
        except GatewayError as error:
            status = error.code
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            if acquired:
                slot.release()
            self.metrics.requests.labels(team, status).inc()
            elapsed = time.monotonic() - started
            self.metrics.latency.labels(team).observe(elapsed)
            await asyncio.shield(
                self.audit_event(
                    "request",
                    request_id,
                    team,
                    status=status,
                    model=request.model,
                    priority=request.priority,
                    elapsed_seconds=elapsed,
                )
            )
