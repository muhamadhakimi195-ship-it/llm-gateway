import asyncio

from gateway.circuit import CircuitBreaker


async def test_open_half_open_single_probe_and_recovery():
    now = [0.0]
    breaker = CircuitBreaker(2, 30, 5, clock=lambda: now[0])
    assert await breaker.enter()
    await breaker.failure()
    assert await breaker.enter()
    await breaker.failure()
    assert await breaker.state() == "open"
    assert not await breaker.enter()
    now[0] = 6.0
    probes = await asyncio.gather(*(breaker.enter() for _ in range(20)))
    assert sum(p is not None for p in probes) == 1
    assert await breaker.state() == "half_open"
    await breaker.success(next(p for p in probes if p is not None))
    assert await breaker.state() == "closed"
    assert await breaker.enter()


async def test_abandoned_half_open_probe_can_be_retried():
    now = [0.0]
    breaker = CircuitBreaker(1, 30, 5, clock=lambda: now[0])
    await breaker.failure()
    now[0] = 6.0
    lease = await breaker.enter()
    assert lease
    await breaker.abandon(lease)
    assert await breaker.enter()


async def test_failure_window_expires_old_failures():
    now = [0.0]
    breaker = CircuitBreaker(2, 5, 10, clock=lambda: now[0])
    await breaker.failure()
    now[0] = 6.0
    await breaker.failure()
    assert await breaker.state() == "closed"


async def test_old_request_cannot_release_another_half_open_probe():
    now = [0.0]
    breaker = CircuitBreaker(1, 30, 5, clock=lambda: now[0])
    old = await breaker.enter()
    await breaker.failure(old)
    now[0] = 6.0
    probe = await breaker.enter()
    await breaker.abandon(old)
    await breaker.success(old)
    assert await breaker.enter() is None
    await breaker.success(probe)
    assert await breaker.state() == "closed"
