"""PostgreSQL table definition and connection settings shared with Alembic.

Defining metadata does not create tables. Only the migration job changes schema.
"""

import os

from sqlalchemy import BigInteger, Column, DateTime, Identity, Index, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL, make_url

metadata = MetaData()

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("kind", String(80), nullable=False),
    Column("request_id", String(64)),
    Column("team", String(80)),
    Column("payload", JSONB, nullable=False),
)
Index("ix_audit_events_request_id", audit_events.c.request_id)
Index("ix_audit_events_team_timestamp", audit_events.c.team, audit_events.c.timestamp)


def database_url(value: str | None = None) -> URL:
    """Normalize the driver without printing secrets or relying on INI interpolation."""
    raw = value if value is not None else os.environ.get("DATABASE_URL", "")
    if not raw:
        raise ValueError("DATABASE_URL is required for PostgreSQL audit storage")
    try:
        url = make_url(raw)
    except Exception:
        raise ValueError("DATABASE_URL must be a valid PostgreSQL connection URL") from None
    if url.drivername not in {"postgresql", "postgresql+psycopg"} or not url.database:
        raise ValueError("DATABASE_URL must identify a PostgreSQL database using psycopg")
    return url.set(drivername="postgresql+psycopg")
