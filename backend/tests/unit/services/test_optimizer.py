from decimal import Decimal

import pytest

from backend.app.domain.enums import MappingStatus, Venue
from backend.app.domain.market import BookLevel
from backend.app.services.fees import FeeEngine, ProbabilityCurveFeeRule
from backend.app.services.optimizer import (
    InsufficientDepth,
    QuoteOptimizer,
    QuotePolicy,
    sweep_cost,
)


def fee_engine() -> FeeEngine:
    engine = FeeEngine()
    engine.register(Venue.KALSHI, "standard", ProbabilityCurveFeeRule("kalshi-v1", Decimal("0.07")))
    engine.register(Venue.POLYMARKET, "standard", ProbabilityCurveFeeRule("poly-v1", Decimal(0)))
    return engine


def test_document_example_calculates_5_54_percent_roi() -> None:
    optimizer = QuoteOptimizer(fee_engine())

    result = optimizer.from_filled_costs(
        quantity=Decimal(268),
        kalshi_cost=Decimal("213.36"),
        kalshi_fee=Decimal("3.05"),
        polymarket_cost=Decimal("37.52"),
        polymarket_fee=Decimal(0),
        explicit_cost=Decimal(0),
        risk_buffer=Decimal(0),
    )

    assert result.profit_floor == Decimal("14.07")
    assert result.conservative_roi.quantize(Decimal("0.0001")) == Decimal("0.0554")


def test_sweep_rejects_insufficient_depth() -> None:
    with pytest.raises(InsufficientDepth):
        sweep_cost([BookLevel(Decimal("0.20"), Decimal(2))], Decimal(3))


def test_probability_curve_fee_rounds_up_to_currency_unit() -> None:
    rule = ProbabilityCurveFeeRule("v1", Decimal("0.07"))

    estimate = rule.estimate(Decimal(1), Decimal("0.50"))

    assert estimate.amount == Decimal("0.02")


def test_optimizer_rejects_non_exact_mapping() -> None:
    optimizer = QuoteOptimizer(fee_engine())
    policy = QuotePolicy(
        minimum_roi=Decimal("0.03"),
        maximum_quantity=Decimal(10),
        quantity_step=Decimal(1),
        explicit_cost=Decimal(0),
        risk_buffer=Decimal(0),
    )

    result = optimizer.optimize(
        mapping_status=MappingStatus.CONDITIONAL,
        kalshi_category="standard",
        polymarket_category="standard",
        kalshi_asks=[BookLevel(Decimal("0.70"), Decimal(10))],
        polymarket_asks=[BookLevel(Decimal("0.20"), Decimal(10))],
        policy=policy,
    )

    assert result.best_quote is None
    assert result.rejection_reasons == ("MAPPING_NOT_EXACT",)
