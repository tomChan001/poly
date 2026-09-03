"""Persist immutable risk policy versions.

Revision ID: 0007_risk_policy_versions
Revises: 0006_fixed_oddpool_endpoint
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_risk_policy_versions"
down_revision: str | None = "0006_fixed_oddpool_endpoint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "risk_policy_version",
        sa.Column("version", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("minimum_roi", sa.Numeric(38, 18), nullable=False),
        sa.Column("maximum_settlement_days", sa.Integer(), nullable=False),
        sa.Column("maximum_book_age_seconds", sa.Numeric(38, 18), nullable=False),
        sa.Column("per_trade_limit", sa.Numeric(38, 18), nullable=False),
        sa.Column("per_event_limit", sa.Numeric(38, 18), nullable=False),
        sa.Column("portfolio_limit", sa.Numeric(38, 18), nullable=False),
        sa.Column("explicit_cost", sa.Numeric(38, 18), nullable=False),
        sa.Column("risk_buffer", sa.Numeric(38, 18), nullable=False),
        sa.Column("maximum_unhedged_seconds", sa.Numeric(38, 18), nullable=False),
        sa.Column("maximum_unhedged_loss", sa.Numeric(38, 18), nullable=False),
        sa.Column("maximum_arrival_gap_seconds", sa.Numeric(38, 18), nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("risk_policy_version")
