"""Add durable execution history projection.

Revision ID: 0003_runtime_execution_history
Revises: 0002_integration_configuration
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_runtime_execution_history"
down_revision: str | None = "0002_integration_configuration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # This immutable-shape projection powers operational recovery and history.
    # The normalized execution ledger remains available for later accounting.
    op.create_table(
        "execution_record",
        sa.Column("correlation_id", sa.String(length=64), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_execution_record_occurred_at",
        "execution_record",
        ["occurred_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_execution_record_occurred_at", table_name="execution_record")
    op.drop_table("execution_record")
