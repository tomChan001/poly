"""Create the initial durable trading and audit schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-18
"""

from collections.abc import Sequence

from alembic import op

from backend.app.db import tables  # noqa: F401
from backend.app.db.base import Base

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EXPECTED_TABLES = {
    "venue_market",
    "rule_version",
    "pair_mapping",
    "mapping_review",
    "discovery_candidate",
    "book_snapshot",
    "quote_evaluation",
    "balance_snapshot",
    "capital_reservation",
    "execution",
    "execution_leg",
    "fill",
    "state_transition",
    "audit_event",
    "outbox_event",
    "system_control",
}


# Database enforcement prevents a future application bug or ad-hoc SQL session
# from rewriting evidence after an execution decision has been made.
AUDIT_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION reject_audit_event_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append-only';
END;
$$ LANGUAGE plpgsql
"""

AUDIT_TRIGGER_SQL = """
CREATE TRIGGER audit_event_append_only
BEFORE UPDATE OR DELETE ON audit_event
FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation()
"""


def upgrade() -> None:
    actual_tables = set(Base.metadata.tables)
    if actual_tables != EXPECTED_TABLES:
        raise RuntimeError(
            "initial migration model drift: "
            f"expected {sorted(EXPECTED_TABLES)}, got {sorted(actual_tables)}"
        )

    Base.metadata.create_all(bind=op.get_bind())
    # asyncpg prepares one statement per execute call and rejects SQL batches.
    # Keeping these commands separate also makes the database failure atomic
    # because Alembic still runs both calls inside the migration transaction.
    op.execute(AUDIT_FUNCTION_SQL)
    op.execute(AUDIT_TRIGGER_SQL)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS reject_audit_event_mutation() CASCADE")
    Base.metadata.drop_all(bind=op.get_bind())
