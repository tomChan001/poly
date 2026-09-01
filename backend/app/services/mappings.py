from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from backend.app.domain.enums import MappingStatus
from backend.app.services.rules import InMemoryRuleStore

REQUIRED_REVIEW_ITEMS = (
    "subject",
    "threshold_boundary",
    "timezone",
    "occurrence_definition",
    "data_source",
    "postponement",
    "cancellation",
    "invalid_result",
    "dispute_process",
    "payout_unit",
)


@dataclass(frozen=True, slots=True)
class MappingReviewRecord:
    mapping_id: UUID
    status: MappingStatus
    reviewer: str
    checklist: dict[str, bool]
    truth_table: list[dict[str, Decimal]]
    notes: str


class MappingReviewService:
    def __init__(self, store: InMemoryRuleStore) -> None:
        self._store = store

    def review(
        self,
        mapping_id: UUID,
        status: MappingStatus,
        reviewer: str,
        checklist: dict[str, bool],
        truth_table: list[dict[str, Decimal]],
        notes: str,
    ) -> MappingReviewRecord:
        if status is MappingStatus.EXACT:
            self._validate_exact(checklist, truth_table)
        elif status not in {MappingStatus.CONDITIONAL, MappingStatus.REJECTED}:
            raise ValueError(f"unsupported review status: {status}")

        mapping = self._store.mappings[mapping_id]
        review = MappingReviewRecord(
            mapping_id,
            status,
            reviewer,
            dict(checklist),
            list(truth_table),
            notes,
        )
        mapping.status = status
        self._store.reviews.append(review)
        return review

    @staticmethod
    def _validate_exact(
        checklist: dict[str, bool],
        truth_table: list[dict[str, Decimal]],
    ) -> None:
        validate_exact_review(checklist, truth_table)


def validate_exact_review(
    checklist: dict[str, bool],
    truth_table: list[dict[str, Decimal]],
) -> None:
    if set(checklist) != set(REQUIRED_REVIEW_ITEMS) or not all(checklist.values()):
        raise ValueError("EXACT review requires a complete checklist")
    if not truth_table:
        raise ValueError("EXACT review requires a truth table")
    for row in truth_table:
        if set(row) != {"kalshi", "polymarket"}:
            raise ValueError("truth table requires both venue payouts")
        if row["kalshi"] + row["polymarket"] != Decimal(1):
            raise ValueError("truth table payouts must sum to 1")
