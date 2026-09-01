"""Add human-reviewed executable market pairs.

Revision ID: 0004_executable_pairs
Revises: 0003_runtime_execution_history
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_executable_pairs"
down_revision: str | None = "0003_runtime_execution_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "executable_pair",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
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
    )
    op.create_index(
        "ix_executable_pair_runtime",
        "executable_pair",
        ["enabled", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_executable_pair_runtime", table_name="executable_pair")
    op.drop_table("executable_pair")
