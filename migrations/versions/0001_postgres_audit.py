"""Create the metadata-only PostgreSQL audit trail."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_postgres_audit"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("request_id", sa.String(64)),
        sa.Column("team", sa.String(80)),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.create_index("ix_audit_events_team_timestamp", "audit_events", ["team", "timestamp"])


def downgrade():
    # Destructive: only run an explicit downgrade after taking a database backup.
    op.drop_index("ix_audit_events_team_timestamp", table_name="audit_events")
    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_table("audit_events")
