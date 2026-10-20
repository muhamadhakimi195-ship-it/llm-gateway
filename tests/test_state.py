import asyncio

import fakeredis.aioredis
import pytest

from gateway.errors import GatewayError
from gateway.schemas import TeamPolicy
from gateway.state import MemoryStore, RedisStore


@pytest.fixture(params=["memory", "redis"])
async def store(request):
    state = (
        MemoryStore() if request.param == "memory" else RedisStore(fakeredis.aioredis.FakeRedis())
    )
    yield state
    await state.close()


async def test_concurrent_admission_never_overdraws_bucket(store):
    policy = TeamPolicy(allowed_models=["balanced"], requests_per_minute=4, tokens_per_minute=1000)

    async def admit():
        try:
            await store.admit("demo", policy, 10)
            return True
        except GatewayError as error:
            assert error.status == 429 and error.retry_after >= 1
            return False

    results = await asyncio.gather(*(admit() for _ in range(50)))
    assert sum(results) == 4


async def test_token_rejection_does_not_consume_request_allowance(store):
    policy = TeamPolicy(allowed_models=["balanced"], requests_per_minute=2, tokens_per_minute=100)
    await store.admit("demo", policy, 90)
    with pytest.raises(GatewayError):
        await store.admit("demo", policy, 20)
    await store.admit("demo", policy, 1)


async def test_concurrent_budget_reservations_and_idempotent_settlement(store):
    policy = TeamPolicy(allowed_models=["balanced"], daily_budget_microusd=100)

    async def reserve():
        try:
            return await store.reserve("demo", policy, 30)
        except GatewayError as error:
            assert error.status == 402
            return None

    reservations = [r for r in await asyncio.gather(*(reserve() for _ in range(20))) if r]
    assert len(reservations) == 3
    assert await store.spent("demo") == 90
    assert await store.settle(reservations[0], 10)
    assert not await store.settle(reservations[0], 0)
    assert await store.spent("demo") == 70
    await store.reserve("demo", policy, 30)
    assert await store.spent("demo") == 100


async def test_oversized_token_bound_rejected_without_consuming_quota(store):
    policy = TeamPolicy(allowed_models=["balanced"], tokens_per_minute=100)
    with pytest.raises(GatewayError) as caught:
        await store.admit("demo", policy, 101)
    assert caught.value.status == 422
    await store.admit("demo", policy, 100)


async def test_policy_seed_does_not_overwrite_admin_update(store):
    initial = TeamPolicy(allowed_models=["balanced"], requests_per_minute=10)
    updated = initial.model_copy(update={"requests_per_minute": 2})
    await store.seed_policy("demo", initial)
    await store.update_policy("demo", updated)
    await store.seed_policy("demo", initial)
    assert (await store.policy("demo")).requests_per_minute == 2


async def test_refill_uses_elapsed_time():
    now = [0.0]
    store = MemoryStore(clock=lambda: now[0])
    policy = TeamPolicy(allowed_models=["balanced"], requests_per_minute=1)
    await store.admit("demo", policy, 1)
    with pytest.raises(GatewayError):
        await store.admit("demo", policy, 1)
    now[0] = 60.0
    await store.admit("demo", policy, 1)


async def test_redis_policies_are_visible_to_other_workers():
    server = fakeredis.FakeServer()
    a = RedisStore(fakeredis.aioredis.FakeRedis(server=server))
    b = RedisStore(fakeredis.aioredis.FakeRedis(server=server))
    policy = TeamPolicy(allowed_models=["balanced"])
    await a.seed_policy("demo", policy)
    await b.update_policy("demo", policy.model_copy(update={"requests_per_minute": 2}))
    assert (await a.policy("demo")).requests_per_minute == 2
    await a.close()
    await b.close()
