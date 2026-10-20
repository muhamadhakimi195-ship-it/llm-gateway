"""Run against a real Redis server, never an emulated one. No FLUSHDB/FLUSHALL."""

import os
import uuid

import pytest
from redis.asyncio import Redis

from gateway.errors import GatewayError
from gateway.schemas import TeamPolicy
from gateway.state import RedisStore

pytestmark = pytest.mark.integration


async def test_real_redis_limits_policy_and_spending_survive_client_restart():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("Set TEST_REDIS_URL to a local Redis server")
    namespace = "gateway-integration-" + uuid.uuid4().hex
    first = RedisStore(Redis.from_url(url), namespace)
    policy = TeamPolicy(
        allowed_models=["balanced"],
        requests_per_minute=1,
        tokens_per_minute=1000,
        daily_budget_microusd=1000,
    )
    try:
        assert await first.health()
        await first.seed_policy("demo", policy)
        await first.admit("demo", policy, 10)
        reservation = await first.reserve("demo", policy, 100)
        assert await first.settle(reservation, 12)
    finally:
        await first.close()
    second = RedisStore(Redis.from_url(url), namespace)
    try:
        # Startup must not overwrite a persisted policy or reset the allowance.
        await second.seed_policy("demo", policy.model_copy(update={"requests_per_minute": 30}))
        persisted = await second.policy("demo")
        assert persisted.requests_per_minute == 1
        assert await second.spent("demo") == 12
        assert not await second.settle(reservation, 0)
        with pytest.raises(GatewayError) as error:
            await second.admit("demo", persisted, 10)
        assert error.value.status == 429
    finally:
        # This random namespace belongs only to this test; preserve all other keys.
        keys = [key async for key in second.client.scan_iter(match=second.key("demo", "*"))]
        if keys:
            await second.client.delete(*keys)
        await second.close()
