"""A per-worker circuit breaker with one concurrent half-open probe."""

import asyncio
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class CircuitLease:
    token: object
    probe: bool


class CircuitBreaker:
    def __init__(self, threshold: int, window: float, cooldown: float, clock=time.monotonic):
        self.threshold, self.window, self.cooldown, self.clock = threshold, window, cooldown, clock
        self.failures: deque[float] = deque()
        self.open_until = 0.0
        self.probe_token = None
        self.lock = asyncio.Lock()

    async def enter(self) -> CircuitLease | None:
        async with self.lock:
            now = self.clock()
            if self.open_until:
                if now < self.open_until or self.probe_token is not None:
                    return None
                self.probe_token = object()
                return CircuitLease(self.probe_token, True)
            return CircuitLease(object(), False)

    async def success(self, lease: CircuitLease | None = None) -> None:
        async with self.lock:
            if self.open_until and (lease is None or lease.token is not self.probe_token):
                return
            self.failures.clear()
            self.open_until = 0.0
            self.probe_token = None

    async def failure(self, lease: CircuitLease | None = None) -> None:
        async with self.lock:
            now = self.clock()
            while self.failures and self.failures[0] < now - self.window:
                self.failures.popleft()
            self.failures.append(now)
            owns_probe = lease is not None and lease.token is self.probe_token
            if owns_probe or len(self.failures) >= self.threshold:
                self.open_until = now + self.cooldown
            if owns_probe:
                self.probe_token = None

    async def abandon(self, lease: CircuitLease | None = None) -> None:
        async with self.lock:
            # Cancellation must not permanently occupy the half-open probe slot.
            if lease is not None and lease.token is self.probe_token:
                self.probe_token = None

    async def state(self) -> str:
        async with self.lock:
            if self.probe_token is not None:
                return "half_open"
            return "open" if self.open_until else "closed"
