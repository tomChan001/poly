"""Persist emergency remediation intent and disposition.

Revision ID: 0008_incident_remediation
Revises: 0007_risk_policy_versions
Create Date: 2026-09-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_incident_remediation"
down_revision: str | None = "0007_risk_policy_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The default backfills existing incidents and keeps fresh metadata-created
    # databases compatible with this upgrade path.
    op.execute(
        "ALTER TABLE execution_incident "
        "ADD COLUMN IF NOT EXISTS remediation_status VARCHAR(32) "
        "NOT NULL DEFAULT 'pending'"
    )
    op.execute(
        "ALTER TABLE execution_incident "
        "ADD COLUMN IF NOT EXISTS remediation_client_order_id VARCHAR(255)"
    )
    op.execute(
        "ALTER TABLE execution_incident "
        "ADD COLUMN IF NOT EXISTS remediation_venue VARCHAR(32)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE execution_incident "
        "DROP COLUMN IF EXISTS remediation_venue"
    )
    op.execute(
        "ALTER TABLE execution_incident "
        "DROP COLUMN IF EXISTS remediation_client_order_id"
    )
    op.execute(
        "ALTER TABLE execution_incident "
        "DROP COLUMN IF EXISTS remediation_status"
    )
