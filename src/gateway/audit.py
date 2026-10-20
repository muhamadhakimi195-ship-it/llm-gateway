"""Durable metadata audit trail. Prompts, completions, and credentials are excluded."""

import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


class AuditStore(Protocol):
    """Service code depends on behaviour, not on SQLite or PostgreSQL."""

    async def write(self, kind, request_id=None, team=None, **payload): ...
    async def recent(self, limit=100): ...
    async def health(self) -> bool: ...
    async def close(self) -> None: ...


def audit_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Audit limit must be an integer between 1 and 1000")
    return limit


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, kind TEXT NOT NULL,
                request_id TEXT, team TEXT, payload TEXT NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS request_idx ON events(request_id)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _write(self, kind, request_id, team, payload):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO events(timestamp,kind,request_id,team,payload) VALUES(?,?,?,?,?)",
                (datetime.now(UTC).isoformat(), kind, request_id, team, json.dumps(payload)),
            )

    async def write(self, kind, request_id=None, team=None, **payload):
        await asyncio.to_thread(self._write, kind, request_id, team, payload)

    async def recent(self, limit=100):
        limit = audit_limit(limit)

        def query():
            with self.connect() as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
                return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

        return await asyncio.to_thread(query)

    async def health(self):
        def query():
            with self.connect() as connection:
                connection.execute("SELECT id FROM events LIMIT 0")
            return True

        return await asyncio.to_thread(query)

    async def close(self):
        # SQLite connections are scoped to each operation; there is no pool to close.
        pass


async def open_audit(path: str | Path | None = None) -> AuditStore:
    """Explicit test paths select SQLite; production selection never silently falls back."""
    backend = "sqlite" if path is not None else os.environ.get("GATEWAY_AUDIT_BACKEND", "sqlite")
    if backend == "sqlite":
        return AuditLog(path or os.environ.get("GATEWAY_AUDIT_PATH", "data/audit.sqlite3"))
    if backend != "postgres":
        raise ValueError("GATEWAY_AUDIT_BACKEND must be sqlite or postgres")
    from gateway.postgres_audit import PostgresAuditLog

    audit = PostgresAuditLog()
    try:
        await audit.health()  # Fail startup if the database or migration is missing.
    except BaseException:
        await audit.close()
        raise
    return audit
