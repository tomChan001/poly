from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.domain.enums import MappingStatus, Venue
from backend.app.domain.market import BookLevel, NormalizedBook
from backend.app.services.executable_pairs import ExecutablePair
from backend.app.services.fees import FeeEngine, ProbabilityCurveFeeRule
from backend.app.services.optimizer import QuoteOptimizer
from backend.app.services.settings import InMemoryRiskPolicyStore, RiskPolicyInput

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def pair() -> ExecutablePair:
    return ExecutablePair(
        id="p1",
        title="Example",
        kalshi_market_id="K1",
        kalshi_outcome="no",
        kalshi_rule_text="Full rules",
        kalshi_rule_url="https://kalshi.com/markets/k1",
        polymarket_market_id="P1",
        polymarket_outcome="yes",
        polymarket_rule_text="Full rules",
        polymarket_rule_url="https://polymarket.com/event/p1",
        minimum_quantity=Decimal(1),
        quantity_step=Decimal(1),
        enabled=True,
        kalshi_category="standard",
        polymarket_category="standard",
        kalshi_expected_settlement_at=NOW + timedelta(days=2),
        polymarket_expected_settlement_at=NOW + timedelta(days=3),
        worst_case_settlement_at=NOW + timedelta(days=3),
    )


class Books:
    def __init__(self, quantity="20", price="0.20", age=0):
        self.quantity = Decimal(quantity)
        self.price = Decimal(price)
        self.age = age

    async def get_books(self, value, now):
        captured = now - timedelta(seconds=self.age)
        return (
            NormalizedBook(
                value.kalshi_market_id,
                "no",
                "k1",
                captured,
                now,
                (BookLevel(Decimal("0.70"), self.quantity),),
            ),
            NormalizedBook(
                value.polymarket_market_id,
                "yes",
                "p1",
                captured,
                now,
                (BookLevel(self.price, Decimal(30)),),
            ),
        )


def optimizer(known=True):
    fees = FeeEngine()
    if known:
        fees.register(
            Venue.KALSHI, "standard", ProbabilityCurveFeeRule("k1", Decimal("0.07"))
        )
        fees.register(
            Venue.POLYMARKET, "standard", ProbabilityCurveFeeRule("p1", Decimal(0))
        )
    return QuoteOptimizer(fees)


@pytest.mark.asyncio
async def test_pending_pair_has_read_only_net_preview_without_becoming_executable():
    from backend.app.services.pair_previews import PairPreviewService

    store = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())
    policy = await store.initialize()
    value = pair()
    preview = await PairPreviewService(optimizer(), Books()).evaluate(
        value, policy, NOW
    )
    assert preview.eligible
    assert preview.quantity > 0
    assert preview.conservative_roi >= policy.minimum_roi
    assert preview.total_fees > 0
    assert preview.paired_liquidity == Decimal(20)
    assert value.status == MappingStatus.PENDING_REVIEW


@pytest.mark.asyncio
async def test_current_policy_roi_and_liquidity_reject_previous_candidate():
    from backend.app.services.pair_previews import PairPreviewService

    store = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())
    policy = await store.initialize()
    service = PairPreviewService(optimizer(), Books())
    assert (await service.evaluate(pair(), policy, NOW)).eligible
    strict = replace(
        policy, minimum_roi=Decimal("0.5"), minimum_liquidity_contracts=Decimal(21)
    )
    result = await service.evaluate(pair(), strict, NOW)
    assert not result.eligible
    assert "INSUFFICIENT_LIQUIDITY" in result.rejection_reasons
    assert "ROI_BELOW_THRESHOLD" in result.rejection_reasons
    assert result.conservative_roi is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"polymarket_expected_settlement_at": None}, "SETTLEMENT_UNKNOWN"),
        ({"worst_case_settlement_at": NOW + timedelta(days=31)}, "SETTLEMENT_TOO_LATE"),
        (
            {"kalshi_expected_settlement_at": NOW - timedelta(days=1)},
            "SETTLEMENT_PASSED",
        ),
    ],
)
async def test_settlement_policy(changes, reason):
    from backend.app.services.pair_previews import PairPreviewService

    policy = await InMemoryRiskPolicyStore().initialize()
    result = await PairPreviewService(optimizer(), Books()).evaluate(
        replace(pair(), **changes), policy, NOW
    )
    assert not result.eligible
    assert reason in result.rejection_reasons


@pytest.mark.asyncio
async def test_unknown_fees_show_prices_but_never_claim_net_roi_or_eligibility():
    from backend.app.services.pair_previews import PairPreviewService

    policy = await InMemoryRiskPolicyStore().initialize()
    result = await PairPreviewService(optimizer(False), Books()).evaluate(
        pair(), policy, NOW
    )
    assert not result.eligible
    assert "FEE_UNKNOWN" in result.rejection_reasons
    assert result.conservative_roi is None
    assert result.gross_roi is not None
    assert result.kalshi_best_ask == Decimal("0.70")


@pytest.mark.asyncio
async def test_stale_or_unavailable_books_do_not_qualify():
    from backend.app.services.pair_previews import PairPreviewService

    policy = await InMemoryRiskPolicyStore().initialize()
    stale = await PairPreviewService(optimizer(), Books(age=30)).evaluate(
        pair(), policy, NOW
    )
    missing = await PairPreviewService(optimizer()).evaluate(pair(), policy, NOW)
    assert not stale.eligible and "STALE_BOOK" in stale.rejection_reasons
    assert (
        not missing.eligible and "NATIVE_DATA_UNAVAILABLE" in missing.rejection_reasons
    )


@pytest.mark.asyncio
async def test_liquidity_counts_only_best_price_not_far_away_depth():
    from backend.app.services.pair_previews import PairPreviewService

    class ThinBooks(Books):
        async def get_books(self, value, now):
            k, p = await super().get_books(value, now)
            return replace(
                k, asks=k.asks + (BookLevel(Decimal("0.99"), Decimal(5000)),)
            ), p

    policy = await InMemoryRiskPolicyStore().initialize()
    policy = replace(policy, minimum_liquidity_contracts=Decimal(5))
    result = await PairPreviewService(optimizer(), ThinBooks(quantity="4")).evaluate(
        pair(), policy, NOW
    )
    assert result.paired_liquidity == Decimal(4)
    assert "INSUFFICIENT_LIQUIDITY" in result.rejection_reasons


@pytest.mark.asyncio
async def test_minimum_order_size_is_part_of_optimization_not_a_post_filter():
    from backend.app.services.pair_previews import PairPreviewService

    class SmallOptimum(Books):
        async def get_books(self, value, now):
            k, p = await super().get_books(value, now)
            levels = (
                BookLevel(Decimal("0.20"), Decimal(1)),
                BookLevel(Decimal("0.55"), Decimal(4)),
            )
            return replace(k, asks=levels), replace(p, asks=levels)

    policy = await InMemoryRiskPolicyStore().initialize()
    policy = replace(policy, minimum_roi=Decimal("0.01"), risk_buffer=Decimal(0))
    value = replace(pair(), minimum_quantity=Decimal(5))
    result = await PairPreviewService(optimizer(), SmallOptimum()).evaluate(
        value, policy, NOW
    )
    assert result.eligible
    assert result.quantity == 5


@pytest.mark.asyncio
async def test_native_fee_metadata_is_resolved_before_loading_fresh_books():
    from backend.app.services.pair_previews import PairPreviewService

    events = []

    class Fees:
        async def optimizer_for(self, value):
            events.append("fees")
            return optimizer()

    class ObservedBooks(Books):
        async def get_books(self, value, now):
            events.append("books")
            return await super().get_books(value, now)

    policy = await InMemoryRiskPolicyStore().initialize()
    result = await PairPreviewService(optimizer(), ObservedBooks(), Fees()).evaluate(
        pair(), policy, NOW
    )
    assert events == ["fees", "books"]
    assert result.eligible


@pytest.mark.asyncio
async def test_capital_cannot_fund_minimum_is_not_mislabeled_as_low_roi():
    from backend.app.services.pair_previews import PairPreviewService

    policy = await InMemoryRiskPolicyStore().initialize()
    policy = replace(
        policy,
        per_trade_limit=Decimal(3),
        minimum_roi=Decimal("0.01"),
        risk_buffer=Decimal(0),
    )
    result = await PairPreviewService(optimizer(), Books()).evaluate(
        replace(pair(), minimum_quantity=Decimal(5)),
        policy,
        NOW,
    )
    assert not result.eligible
    assert "ROI_BELOW_THRESHOLD" not in result.rejection_reasons
    assert any(
        reason in result.rejection_reasons
        for reason in ("KALSHI_BALANCE_INSUFFICIENT", "PER_TRADE_LIMIT")
    )


def test_preview_expiration_keeps_evidence_but_removes_eligibility():
    from backend.app.services.pair_previews import PairPreview, expire_preview

    preview = PairPreview(
        eligible=True,
        evaluated_at=NOW,
        risk_policy_version="v1",
        valid_until=NOW + timedelta(seconds=2),
        conservative_roi=Decimal("0.1"),
    )
    result = expire_preview(preview, NOW + timedelta(seconds=3))
    assert not result.eligible
    assert result.rejection_reasons == ("STALE_BOOK",)
    assert result.conservative_roi == Decimal("0.1")


@pytest.mark.asyncio
async def test_preview_uses_receipt_freshness_without_inventing_exchange_timestamp():
    from backend.app.services.pair_previews import PairPreviewService

    class RestBooks(Books):
        async def get_books(self, value, now):
            k, p = await super().get_books(value, now)
            return replace(k, captured_at=None), p

    policy = await InMemoryRiskPolicyStore().initialize()
    result = await PairPreviewService(optimizer(), RestBooks()).evaluate(
        pair(), policy, NOW
    )
    assert result.eligible
    assert result.valid_until == NOW + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_slow_market_does_not_expire_healthy_market_in_batch():
    import asyncio

    from backend.app.services.pair_previews import PairPreviewService

    class MixedBooks(Books):
        async def get_books(self, value, now):
            if value.id == "slow":
                await asyncio.sleep(0.4)
                raise ValueError("unavailable")
            return await super().get_books(value, datetime.now(UTC))

    policy = replace(
        await InMemoryRiskPolicyStore().initialize(),
        maximum_book_age_seconds=Decimal("0.2"),
    )
    results = await PairPreviewService(optimizer(), MixedBooks()).evaluate_many(
        [pair(), replace(pair(), id="slow")],
        policy,
    )
    assert results[0].eligible
    assert results[0].valid_until > datetime.now(UTC)
    assert not results[1].eligible


@pytest.mark.asyncio
async def test_large_batch_rotates_healthy_candidates_instead_of_starving_tail():
    import asyncio

    from backend.app.services.pair_previews import PairPreviewService

    class HealthyBooks(Books):
        async def get_books(self, value, now):
            await asyncio.sleep(0.08)
            return await super().get_books(value, datetime.now(UTC))

    policy = replace(
        await InMemoryRiskPolicyStore().initialize(),
        maximum_book_age_seconds=Decimal("0.2"),
    )
    candidates = [replace(pair(), id=f"p{index}") for index in range(32)]
    service = PairPreviewService(optimizer(), HealthyBooks())
    first = await service.evaluate_many(candidates, policy)
    second = await service.evaluate_many(candidates, policy)
    first_ids = {index for index, preview in enumerate(first) if preview.eligible}
    second_ids = {index for index, preview in enumerate(second) if preview.eligible}
    assert first_ids
    assert second_ids - first_ids
    assert any("PREVIEW_PENDING" in preview.rejection_reasons for preview in first)
