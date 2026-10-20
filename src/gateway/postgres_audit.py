"""Async, pooled PostgreSQL audit storage. Never creates schema at API startup."""

from datetime import UTC, datetime

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.audit import audit_limit
from gateway.database import audit_events, database_url


class PostgresAuditLog:
    def __init__(self, url: str | None = None):
        self.engine = create_async_engine(
            database_url(url),
            pool_size=5,
            max_overflow=5,
            pool_timeout=3,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"connect_timeout": 3, "options": "-c statement_timeout=5000"},
        )

    async def write(self, kind, request_id=None, team=None, **payload):
        # One short transaction per event. Never hold a DB connection during inference.
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(audit_events).values(
                    timestamp=datetime.now(UTC),
                    kind=kind,
                    request_id=request_id,
                    team=team,
                    payload=payload,
                )
            )

    async def recent(self, limit=100):
        statement = (
            select(audit_events).order_by(audit_events.c.id.desc()).limit(audit_limit(limit))
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return [{**dict(row), "timestamp": row["timestamp"].isoformat()} for row in rows]

    async def health(self):
        async with self.engine.connect() as connection:
            # SELECT 1 alone would miss an unmigrated database.
            await connection.execute(select(audit_events.c.id).limit(0))
        return True

    async def close(self):
        await self.engine.dispose()
