"""Persist the minimum best-ask liquidity threshold for every risk policy.

Revision ID: 0010_risk_policy_liquidity
Revises: 0009_execution_capital_settlement
Create Date: 2026-09-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_risk_policy_liquidity"
down_revision: str | None = "0009_execution_capital_settlement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Preserve old versions with a safe threshold; tolerate current metadata
    # having already created this column during a fresh database initialization.
    op.execute(
        "ALTER TABLE risk_policy_version "
        "ADD COLUMN IF NOT EXISTS minimum_liquidity_contracts NUMERIC(38, 18) "
        "NOT NULL DEFAULT 1"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE risk_policy_version DROP COLUMN IF EXISTS minimum_liquidity_contracts"
    )
