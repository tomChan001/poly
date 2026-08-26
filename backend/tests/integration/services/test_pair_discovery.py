from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.adapters.oddpool.schema import OddpoolResponse
from backend.app.domain.enums import MappingStatus
from backend.app.services.executable_pairs import (
    ExecutablePairInput,
    ExecutablePairService,
    InMemoryExecutablePairRepository,
)
from backend.app.services.mappings import REQUIRED_REVIEW_ITEMS
from backend.app.services.pair_discovery import OddpoolPairDiscoveryService

NOW = datetime(2026, 8, 21, 2, 0, tzinfo=UTC)


def response(updated_at: datetime) -> OddpoolResponse:
    return OddpoolResponse.model_validate(
        {
            "opportunities": [
                {
                    "id": "oddpool-001",
                    "title": "Will the event happen?",
                    "outcome": "complementary",
                    "updated_at": updated_at.isoformat(),
                    "gross_spread": "0.10",
                    "estimated_fees": "0.01",
                    "legs": [
                        {
                            "venue": "kalshi",
                            "outcome": "no",
                            "market_url": "https://kalshi.com/markets/K-EVENT",
                            "display_price": "0.70",
                        },
                        {
                            "venue": "polymarket",
                            "outcome": "yes",
                            "market_url": "https://polymarket.com/event/event-slug",
                            "display_price": "0.20",
                        },
                    ],
                }
            ]
        }
    )


class Source:
    def __init__(self) -> None:
        self.payload = response(NOW)

    async def fetch_opportunities(self) -> OddpoolResponse:
        return self.payload


class Resolver:
    def __init__(self) -> None:
        self.title = "Will the event happen?"
        self.kalshi_rule_text = "Kalshi native rule"
        self.kalshi_rule_url = "https://kalshi.com/markets/K-EVENT"
        self.polymarket_rule_text = "Polymarket native rule"
        self.enabled = True

    async def resolve(self, _opportunity) -> ExecutablePairInput:
        return ExecutablePairInput(
            title=self.title,
            kalshi_market_id="K-EVENT",
            kalshi_outcome="no",
            kalshi_rule_text=self.kalshi_rule_text,
            kalshi_rule_url=self.kalshi_rule_url,
            polymarket_market_id="token-yes",
            polymarket_outcome="yes",
            polymarket_rule_text=self.polymarket_rule_text,
            polymarket_rule_url="https://polymarket.com/event/event-slug",
            minimum_quantity=Decimal(5),
            quantity_step=Decimal(1),
            enabled=self.enabled,
            kalshi_expected_settlement_at=NOW,
            polymarket_expected_settlement_at=NOW + timedelta(days=2),
            worst_case_settlement_at=NOW + timedelta(days=2),
            kalshi_category="politics",
            polymarket_category="news",
            kalshi_minimum_tick=Decimal("0.01"),
            polymarket_minimum_tick=Decimal("0.001"),
            native_fingerprint="native-v1",
            material_fingerprint=(
                f"{self.kalshi_rule_text}|{self.polymarket_rule_text}"
            ),
        )


@pytest.mark.asyncio
async def test_oddpool_candidates_automatically_become_idempotent_pending_pairs() -> None:
    source = Source()
    resolver = Resolver()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    discovery = OddpoolPairDiscoveryService(source, resolver, pairs)

    first = await discovery.run_once()
    duplicate = await discovery.run_once()
    [pair] = await pairs.list()

    assert first.imported == 1
    assert duplicate.imported == 0
    assert pair.status is MappingStatus.PENDING_REVIEW
    assert pair.source_candidate_id == "oddpool-001"
    assert pair.source_updated_at == NOW

    reviewed = await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="human review",
        reviewer="human",
    )
    assert reviewed.status is MappingStatus.EXACT

    source.payload = response(NOW + timedelta(minutes=1))
    unchanged = await discovery.run_once()
    [still_reviewed] = await pairs.list()

    resolver.kalshi_rule_text = "Kalshi native rule v2"
    changed = await discovery.run_once()
    [updated] = await pairs.list()

    assert unchanged.updated == 0
    assert unchanged.duplicates == 1
    assert still_reviewed.status is MappingStatus.EXACT
    assert changed.updated == 1
    assert updated.id == pair.id
    assert updated.status is MappingStatus.PENDING_REVIEW
    assert updated.reviewed_by is None
    assert updated.kalshi_rule_text == "Kalshi native rule v2"
    assert updated.source_updated_at == NOW + timedelta(minutes=1)


@pytest.mark.asyncio
async def test_non_material_discovery_change_preserves_exact_review() -> None:
    source = Source()
    resolver = Resolver()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    discovery = OddpoolPairDiscoveryService(source, resolver, pairs)

    await discovery.run_once()
    [pair] = await pairs.list()
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="human review",
        reviewer="human",
    )

    resolver.title = "Will the event happen later?"
    resolver.kalshi_rule_url = "https://kalshi.com/markets/K-EVENT?relinked=1"
    resolver.enabled = False

    result = await discovery.run_once()
    [updated] = await pairs.list()

    assert result.updated == 1
    assert result.duplicates == 0
    assert updated.status is MappingStatus.EXACT
    assert updated.reviewed_by == "human"
    assert updated.title == "Will the event happen later?"
    assert updated.kalshi_rule_url.endswith("relinked=1")
    assert updated.enabled is False


@pytest.mark.asyncio
async def test_bad_candidate_does_not_block_other_automatic_candidates() -> None:
    source = Source()
    good = source.payload.opportunities[0].model_copy(update={"id": "oddpool-good"})
    bad = source.payload.opportunities[0].model_copy(update={"id": "oddpool-bad"})
    source.payload = OddpoolResponse(opportunities=[bad, good])

    class SelectiveResolver(Resolver):
        async def resolve(self, opportunity) -> ExecutablePairInput:
            if opportunity.id == "oddpool-bad":
                raise ValueError("market link is ambiguous")
            return await super().resolve(opportunity)

    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    result = await OddpoolPairDiscoveryService(source, SelectiveResolver(), pairs).run_once()

    assert result.imported == 1
    assert result.failed == 1
    assert result.errors == ("oddpool-bad: market link is ambiguous",)
    [saved] = await pairs.list()
    assert saved.source_candidate_id == "oddpool-good"
