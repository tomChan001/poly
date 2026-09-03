"""Index durable execution-capital settlement state for recovery.

Revision ID: 0009_execution_capital_settlement
Revises: 0008_incident_remediation
Create Date: 2026-09-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_execution_capital_settlement"
down_revision: str | None = "0008_incident_remediation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE execution_record "
        "ADD COLUMN IF NOT EXISTS capital_settled BOOLEAN"
    )
    op.execute(
        """
        UPDATE execution_record AS record
        SET capital_settled = CASE
            WHEN record.state = 'submitted' THEN FALSE
            WHEN record.state IN ('paired', 'partially_hedged', 'exception', 'cancelled')
                THEN NOT EXISTS (
                    SELECT 1
                    FROM capital_reservation AS reservation
                    WHERE reservation.correlation_id = record.correlation_id
                      AND reservation.status = 'active'
                )
            ELSE FALSE
        END
        WHERE record.capital_settled IS NULL
        """
    )
    op.execute(
        "ALTER TABLE execution_record "
        "ALTER COLUMN capital_settled SET DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE execution_record "
        "ALTER COLUMN capital_settled SET NOT NULL"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_execution_record_recovery_candidates
        ON execution_record (occurred_at DESC)
        WHERE state = 'submitted' OR capital_settled = FALSE
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_execution_record_recovery_candidates")
    op.execute(
        "ALTER TABLE execution_record "
        "DROP COLUMN IF EXISTS capital_settled"
    )
