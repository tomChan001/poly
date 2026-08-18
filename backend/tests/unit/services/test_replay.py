from datetime import UTC, datetime, timedelta

from backend.app.services.replay import TimestampedEvidence, evidence_available_at


def test_replay_excludes_evidence_received_after_decision() -> None:
    decision_at = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)
    evidence = [
        TimestampedEvidence(decision_at - timedelta(milliseconds=1), "old-book"),
        TimestampedEvidence(decision_at + timedelta(milliseconds=1), "future-book"),
    ]

    available = evidence_available_at(evidence, decision_at)

    assert [item.value for item in available] == ["old-book"]

