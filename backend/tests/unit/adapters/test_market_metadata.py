from decimal import Decimal

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
            "rules_url": "https://kalshi.com/markets/KX-EXAMPLE",
            "tick_size": "0.01",
            "minimum_order_size": "1",
        }
    )

    assert market.venue is Venue.KALSHI
    assert market.external_id == "KX-EXAMPLE"
    assert market.minimum_tick == Decimal("0.01")


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
