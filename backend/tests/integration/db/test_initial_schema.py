from backend.app.db import tables  # noqa: F401
from backend.app.db.base import Base


def test_initial_schema_contains_all_durable_business_tables() -> None:
    expected = {
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
        "risk_policy_version",
    }

    assert set(Base.metadata.tables) == expected
