from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TimestampedEvidence[T]:
    received_at: datetime
    value: T


def evidence_available_at[T](
    evidence: list[TimestampedEvidence[T]],
    decision_at: datetime,
) -> list[TimestampedEvidence[T]]:
    """Exclude future knowledge when replaying an execution decision."""
    return [item for item in evidence if item.received_at <= decision_at]
