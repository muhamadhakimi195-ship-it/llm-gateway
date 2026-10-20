"""Atomic quota and spending controls with interchangeable local and Redis backends.

Budget reservations debit the ledger *before* a provider call. A worker crash therefore
leaves a conservative debit rather than silently restoring funds that might be spent.
"""

import asyncio
import math
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from redis.asyncio import Redis

from gateway.errors import GatewayError
from gateway.schemas import TeamPolicy


def utc_day() -> str:
    return datetime.now(UTC).date().isoformat()


@dataclass(frozen=True)
class Reservation:
    id: str
    team: str
    day: str
    amount: int


class StateStore(Protocol):
    async def seed_policy(self, team: str, policy: TeamPolicy) -> None: ...
    async def policy(self, team: str) -> TeamPolicy: ...
    async def update_policy(self, team: str, policy: TeamPolicy) -> None: ...
    async def admit(self, team: str, policy: TeamPolicy, tokens: int) -> None: ...
    async def reserve(self, team: str, policy: TeamPolicy, amount: int) -> Reservation: ...
    async def settle(self, reservation: Reservation, actual: int) -> bool: ...
    async def spent(self, team: str) -> int: ...
    async def health(self) -> bool: ...
    async def close(self) -> None: ...


class MemoryStore:
    """Single-process backend for development; the lock is not a distributed lock."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = asyncio.Lock()
        self.policies: dict[str, TeamPolicy] = {}
        self.buckets: dict[str, tuple[float, float, float]] = {}
        self.ledger: dict[tuple[str, str], int] = {}
        self.reservations: dict[str, Reservation] = {}

    async def seed_policy(self, team, policy):
        async with self.lock:
            self.policies.setdefault(team, policy.model_copy(deep=True))

    async def policy(self, team):
        async with self.lock:
            return self.policies[team].model_copy(deep=True)

    async def update_policy(self, team, policy):
        async with self.lock:
            self.policies[team] = policy.model_copy(deep=True)

    async def admit(self, team, policy, tokens):
        if tokens > policy.tokens_per_minute:
            raise GatewayError(
                "token_bound_exceeds_quota", "Request token bound exceeds team capacity", 422
            )
        async with self.lock:
            now = self.clock()
            rpm, tpm = policy.requests_per_minute, policy.tokens_per_minute
            requests, available_tokens, last = self.buckets.get(team, (rpm, tpm, now))
            elapsed = max(0, now - last)
            requests = min(rpm, requests + elapsed * rpm / 60)
            available_tokens = min(tpm, available_tokens + elapsed * tpm / 60)
            if requests < 1 or available_tokens < tokens:
                wait = max((1 - requests) * 60 / rpm, (tokens - available_tokens) * 60 / tpm)
                self.buckets[team] = (requests, available_tokens, now)
                raise GatewayError(
                    "rate_limited", "Team rate limit exceeded", 429, max(1, math.ceil(wait))
                )
            self.buckets[team] = (requests - 1, available_tokens - tokens, now)

    async def reserve(self, team, policy, amount):
        reservation = Reservation(uuid.uuid4().hex, team, utc_day(), amount)
        async with self.lock:
            key = (team, reservation.day)
            spent = self.ledger.get(key, 0)
            if spent + amount > policy.daily_budget_microusd:
                raise GatewayError(
                    "budget_exceeded", "Team daily budget cannot cover this attempt", 402
                )
            self.ledger[key] = spent + amount
            self.reservations[reservation.id] = reservation
        return reservation

    async def settle(self, reservation, actual):
        async with self.lock:
            original = self.reservations.pop(reservation.id, None)
            if original is None:
                return False
            key = (original.team, original.day)
            self.ledger[key] += actual - original.amount
            return True

    async def spent(self, team):
        async with self.lock:
            return self.ledger.get((team, utc_day()), 0)

    async def health(self):
        return True

    async def close(self):
        pass


# Both request and token allowances are checked together: a rejected token check
# must not consume a request allowance. Redis TIME avoids worker-clock skew.
RATE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local rpm, tpm, wanted = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local last = tonumber(redis.call('HGET', KEYS[1], 'time')) or now
local req = tonumber(redis.call('HGET', KEYS[1], 'requests')) or rpm
local tok = tonumber(redis.call('HGET', KEYS[1], 'tokens')) or tpm
local elapsed = math.max(0, now-last)
req = math.min(rpm, req + elapsed*rpm/60)
tok = math.min(tpm, tok + elapsed*tpm/60)
local wait = math.max(0, (1-req)*60/rpm, (wanted-tok)*60/tpm)
if req >= 1 and tok >= wanted then
    redis.call('HSET', KEYS[1], 'requests', req-1, 'tokens', tok-wanted, 'time', now)
    redis.call('EXPIRE', KEYS[1], 120)
    return 0
end
redis.call('HSET', KEYS[1], 'requests', req, 'tokens', tok, 'time', now)
redis.call('EXPIRE', KEYS[1], 120)
return math.max(1, math.ceil(wait))
"""

RESERVE_LUA = """
local amount, cap = tonumber(ARGV[1]), tonumber(ARGV[2])
local spent = tonumber(redis.call('GET', KEYS[1])) or 0
if spent + amount > cap then return 0 end
if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
redis.call('INCRBY', KEYS[1], amount)
redis.call('EXPIRE', KEYS[1], 259200)
redis.call('SET', KEYS[2], amount, 'EX', 259200)
return 1
"""

SETTLE_LUA = """
local original = redis.call('GET', KEYS[2])
if not original then return 0 end
redis.call('INCRBY', KEYS[1], tonumber(ARGV[1]) - tonumber(original))
redis.call('EXPIRE', KEYS[1], 259200)
redis.call('DEL', KEYS[2])
return 1
"""


class RedisStore:
    """Shared control plane. Hash tags keep related keys in one Redis Cluster slot."""

    def __init__(self, client: Redis, namespace: str = "llmgw"):
        self.client, self.namespace = client, namespace
        self.rate_script = client.register_script(RATE_LUA)
        self.reserve_script = client.register_script(RESERVE_LUA)
        self.settle_script = client.register_script(SETTLE_LUA)

    def key(self, team, suffix):
        return f"{self.namespace}:{{{team}}}:{suffix}"

    async def seed_policy(self, team, policy):
        await self.client.set(self.key(team, "policy"), policy.model_dump_json(), nx=True)

    async def policy(self, team):
        raw = await self.client.get(self.key(team, "policy"))
        if raw is None:
            raise RuntimeError("Missing team policy")
        return TeamPolicy.model_validate_json(raw)

    async def update_policy(self, team, policy):
        await self.client.set(self.key(team, "policy"), policy.model_dump_json())

    async def admit(self, team, policy, tokens):
        if tokens > policy.tokens_per_minute:
            raise GatewayError(
                "token_bound_exceeds_quota", "Request token bound exceeds team capacity", 422
            )
        wait = int(
            await self.rate_script(
                keys=[self.key(team, "rate")],
                args=[policy.requests_per_minute, policy.tokens_per_minute, tokens],
            )
        )
        if wait:
            raise GatewayError("rate_limited", "Team rate limit exceeded", 429, wait)

    async def reserve(self, team, policy, amount):
        reservation = Reservation(uuid.uuid4().hex, team, utc_day(), amount)
        result = int(
            await self.reserve_script(
                keys=[
                    self.key(team, f"spent:{reservation.day}"),
                    self.key(team, f"lease:{reservation.id}"),
                ],
                args=[amount, policy.daily_budget_microusd],
            )
        )
        if result != 1:
            raise GatewayError(
                "budget_exceeded", "Team daily budget cannot cover this attempt", 402
            )
        return reservation

    async def settle(self, reservation, actual):
        return bool(
            await self.settle_script(
                keys=[
                    self.key(reservation.team, f"spent:{reservation.day}"),
                    self.key(reservation.team, f"lease:{reservation.id}"),
                ],
                args=[actual],
            )
        )

    async def spent(self, team):
        return int(await self.client.get(self.key(team, f"spent:{utc_day()}")) or 0)

    async def health(self):
        return bool(await self.client.ping())

    async def close(self):
        await self.client.aclose()
