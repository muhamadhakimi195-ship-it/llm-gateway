"""Explicit loop factories avoid deprecated global event-loop policies.

Psycopg's async sockets require a SelectorEventLoop on Windows. Linux keeps
its normal asyncio loop. This single-worker reference does not launch subprocesses
from its request loop.
"""

import asyncio
import sys


def loop_factory():
    """Uvicorn/Runner hook: create a concrete loop, not a global loop policy."""
    return asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()


def run_async(coroutine):
    """Python 3.11+ command entry point, also used by Alembic and integration tests."""
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(coroutine)
