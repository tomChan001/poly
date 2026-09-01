from decimal import Decimal

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
