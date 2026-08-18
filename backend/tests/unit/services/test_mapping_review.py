from decimal import Decimal

import pytest

from backend.app.domain.enums import MappingStatus
from backend.app.services.mappings import REQUIRED_REVIEW_ITEMS, MappingReviewService
from backend.app.services.rules import InMemoryRuleStore, RuleService


def build_pending_mapping():
    store = InMemoryRuleStore()
    rules = RuleService(store)
    kalshi = rules.record_version("kalshi-market", "rule K", "https://kalshi/rule")
    poly = rules.record_version("poly-market", "rule P", "https://poly/rule")
    mapping = store.create_mapping(
        "kalshi-market",
        "poly-market",
        kalshi.id,
        poly.id,
        MappingStatus.PENDING_REVIEW,
    )
    return store, mapping


def test_exact_review_requires_complete_checklist() -> None:
    store, mapping = build_pending_mapping()
    service = MappingReviewService(store)
    checklist = dict.fromkeys(REQUIRED_REVIEW_ITEMS, True)
    checklist["timezone"] = False

    with pytest.raises(ValueError, match="checklist"):
        service.review(
            mapping.id,
            MappingStatus.EXACT,
            "reviewer@example.com",
            checklist,
            [{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
            "",
        )


def test_exact_review_requires_complementary_truth_table() -> None:
    store, mapping = build_pending_mapping()
    service = MappingReviewService(store)

    with pytest.raises(ValueError, match="sum to 1"):
        service.review(
            mapping.id,
            MappingStatus.EXACT,
            "reviewer@example.com",
            dict.fromkeys(REQUIRED_REVIEW_ITEMS, True),
            [{"kalshi": Decimal(1), "polymarket": Decimal(1)}],
            "",
        )

