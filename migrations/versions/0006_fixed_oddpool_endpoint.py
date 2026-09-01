"""Normalize the fixed Oddpool production endpoint.

Revision ID: 0006_fixed_oddpool_endpoint
Revises: 0005_prelive_safety
Create Date: 2026-09-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_fixed_oddpool_endpoint"
down_revision: str | None = "0005_prelive_safety"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE integration_config "
        "SET base_url = 'https://api.oddpool.com', version = version + 1 "
        "WHERE provider = 'oddpool' "
        "AND base_url <> 'https://api.oddpool.com'"
    )


def downgrade() -> None:
    # The previous operator-supplied URL is intentionally not recoverable.
    pass
