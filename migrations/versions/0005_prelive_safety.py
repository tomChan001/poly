"""Add durable pre-live safety evidence.

Revision ID: 0005_prelive_safety
Revises: 0004_executable_pairs
Create Date: 2026-08-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_prelive_safety"
down_revision: str | None = "0004_executable_pairs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Fresh databases are created from current metadata by 0001; IF NOT EXISTS
    # also supports databases that were created before this evidence field.
    op.execute(
        "ALTER TABLE capital_reservation "
        "ADD COLUMN IF NOT EXISTS event_id VARCHAR(255)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_capital_reservation_exposure "
        "ON capital_reservation (status, event_id, venue)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_incident (
            idempotency_key VARCHAR(255) PRIMARY KEY,
            correlation_id VARCHAR(64) NOT NULL,
            state VARCHAR(32) NOT NULL,
            action VARCHAR(64) NOT NULL,
            simulated BOOLEAN NOT NULL,
            unhedged_quantity VARCHAR(128) NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS execution_incident")
    op.execute("DROP INDEX IF EXISTS ix_capital_reservation_exposure")
    op.execute("ALTER TABLE capital_reservation DROP COLUMN IF EXISTS event_id")
