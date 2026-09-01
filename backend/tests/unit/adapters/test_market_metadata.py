from decimal import Decimal
from typing import Any

import pytest

from backend.app.adapters.kalshi.markets import normalize_kalshi_market
from backend.app.adapters.polymarket.markets import normalize_polymarket_market
from backend.app.domain.enums import Venue


def test_kalshi_market_uses_stable_ticker_and_decimal_strings() -> None:
    market = normalize_kalshi_market(
        {
            "ticker": "KX-EXAMPLE",
            "title": "Example?",
            "status": "open",
            "rules_primary": "Resolves yes when example occurs.",
            "rules_url": " https://kalshi.com/markets/custom-rule-page ",
            "tick_size": "0.01",
            "minimum_order_size": "1",
        }
    )

    assert market.venue is Venue.KALSHI
    assert market.external_id == "KX-EXAMPLE"
    assert market.rule_url == "https://kalshi.com/markets/custom-rule-page"
    assert market.minimum_tick == Decimal("0.01")


def test_kalshi_market_uses_current_price_ranges_and_default_quantity() -> None:
    market = normalize_kalshi_market(
        {
            "ticker": "KX-CURRENT",
            "title": "Current response?",
            "status": "open",
            "rules_primary": "Resolves yes when the event occurs.",
            "price_level_structure": "linear_cent",
            "price_ranges": [
                {"start": "0.0000", "end": "1.0000", "step": "0.0100"}
            ],
        }
    )

    assert market.minimum_tick == Decimal("0.0100")
    assert market.minimum_quantity == Decimal(1)


def test_kalshi_market_prefers_legacy_increments_over_malformed_ranges() -> None:
    payload = _current_kalshi_payload()
    payload.update(
        {
            "tick_size": "0.005",
            "minimum_order_size": "2",
            "price_ranges": "malformed",
        }
    )

    market = normalize_kalshi_market(payload)

    assert market.minimum_tick == Decimal("0.005")
    assert market.minimum_quantity == Decimal(2)


def test_kalshi_market_uses_smallest_step_from_tapered_ranges() -> None:
    payload = _current_kalshi_payload()
    payload["price_ranges"] = [
        {"start": "0.00", "end": "0.10", "step": "0.01"},
        {"start": "0.10", "end": "1.00", "step": "0.05"},
    ]

    market = normalize_kalshi_market(payload)

    assert market.minimum_tick == Decimal("0.01")


@pytest.mark.parametrize(
    "tick_size",
    [
        pytest.param("0", id="zero"),
        pytest.param("-0.01", id="negative"),
        pytest.param("bad", id="invalid-decimal"),
        pytest.param(0.01, id="float"),
        pytest.param(True, id="boolean"),
    ],
)
def test_kalshi_market_rejects_invalid_legacy_tick_size(
    tick_size: object,
) -> None:
    payload = _current_kalshi_payload()
    payload["tick_size"] = tick_size
    payload["minimum_order_size"] = "1"

    with pytest.raises((TypeError, ValueError)):
        normalize_kalshi_market(payload)


@pytest.mark.parametrize(
    "price_ranges",
    [
        pytest.param([], id="empty"),
        pytest.param(["not-a-range"], id="entry-not-dict"),
        pytest.param(
            [{"start": "0", "end": "1", "step": "0"}],
            id="zero-step",
        ),
        pytest.param(
            [{"start": "0.5", "end": "0.5", "step": "0.01"}],
            id="start-equals-end",
        ),
        pytest.param(
            [{"start": "-0.1", "end": "0.5", "step": "0.01"}],
            id="negative-start",
        ),
        pytest.param(
            [{"start": "0", "end": "1.1", "step": "0.01"}],
            id="end-above-one",
        ),
        pytest.param(
            [{"start": "0", "end": "0.1", "step": "0.2"}],
            id="step-exceeds-width",
        ),
        pytest.param(
            [{"start": "0", "end": "1", "step": "bad"}],
            id="invalid-decimal",
        ),
        pytest.param(
            [{"start": "0", "end": "1", "step": 0.01}],
            id="float-step",
        ),
    ],
)
def test_kalshi_market_rejects_invalid_price_ranges(
    price_ranges: object,
) -> None:
    payload = _current_kalshi_payload()
    payload["price_ranges"] = price_ranges

    with pytest.raises((TypeError, ValueError)):
        normalize_kalshi_market(payload)


@pytest.mark.parametrize(
    "minimum_order_size",
    [
        pytest.param("0", id="zero"),
        pytest.param("-1", id="negative"),
        pytest.param("bad", id="invalid-decimal"),
        pytest.param(1.0, id="float"),
        pytest.param(True, id="boolean"),
    ],
)
def test_kalshi_market_rejects_invalid_legacy_minimum_order_size(
    minimum_order_size: object,
) -> None:
    payload = _current_kalshi_payload()
    payload["minimum_order_size"] = minimum_order_size

    with pytest.raises((TypeError, ValueError)):
        normalize_kalshi_market(payload)


def test_kalshi_market_preserves_valid_legacy_minimum_order_size() -> None:
    payload = _current_kalshi_payload()
    payload["minimum_order_size"] = "3"

    market = normalize_kalshi_market(payload)

    assert market.minimum_quantity == Decimal(3)


def test_kalshi_market_derives_rule_url_when_api_omits_it() -> None:
    market = normalize_kalshi_market(
        {
            "ticker": "KXBOXING-26SEP19FMAYMPAC-FMAY",
            "title": "Will Floyd Mayweather beat Manny Pacquiao?",
            "status": "open",
            "rules_primary": "Resolves yes if Floyd Mayweather wins the bout.",
            "rules_secondary": "Official results determine the outcome.",
            "tick_size": "0.01",
            "minimum_order_size": "1",
        }
    )

    assert market.rule_text == "Resolves yes if Floyd Mayweather wins the bout."
    assert market.rule_url == (
        "https://kalshi.com/markets/KXBOXING-26SEP19FMAYMPAC-FMAY"
    )


@pytest.mark.parametrize("rules_primary", [None, "", "   "])
def test_kalshi_market_requires_non_empty_primary_rules(
    rules_primary: object,
) -> None:
    with pytest.raises(
        TypeError,
        match="^Kalshi rules_primary must be non-empty text$",
    ):
        normalize_kalshi_market(
            {
                "ticker": "KX-EXAMPLE",
                "title": "Example?",
                "status": "open",
                "rules_primary": rules_primary,
                "rules_secondary": "Additional resolution details.",
                "rules_url": "https://kalshi.com/markets/KX-EXAMPLE",
                "tick_size": "0.01",
                "minimum_order_size": "1",
            }
        )


def test_polymarket_market_uses_condition_id_not_page_url() -> None:
    market = normalize_polymarket_market(
        {
            "condition_id": "0xcondition",
            "question": "Example?",
            "active": True,
            "description": "Resolves yes when example occurs.",
            "url": "https://polymarket.com/event/example",
            "minimum_tick_size": "0.001",
            "minimum_order_size": "5",
        }
    )

    assert market.venue is Venue.POLYMARKET
    assert market.external_id == "0xcondition"
    assert market.minimum_quantity == Decimal(5)


def _current_kalshi_payload() -> dict[str, Any]:
    return {
        "ticker": "KX-CURRENT",
        "title": "Current response?",
        "status": "open",
        "rules_primary": "Resolves yes when the event occurs.",
        "price_level_structure": "linear_cent",
        "price_ranges": [
            {"start": "0.0000", "end": "1.0000", "step": "0.0100"}
        ],
    }
