import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from backend.app.adapters.kalshi.orderbook import parse_kalshi_book
from backend.app.adapters.polymarket.orderbook import parse_polymarket_book


def test_kalshi_no_asks_are_derived_from_yes_bids() -> None:
    payload = json.loads(Path("backend/tests/fixtures/kalshi/orderbook.json").read_text())

    book = parse_kalshi_book(
        market_id="K-1",
        outcome="no",
        payload=payload,
        captured_at=datetime(2026, 8, 18, 1, 0, tzinfo=UTC),
    )

    assert book.asks[0].price == Decimal("0.76")
    assert book.asks[0].quantity == Decimal(505)


def test_polymarket_asks_are_normalized() -> None:
    payload = json.loads(Path("backend/tests/fixtures/polymarket/orderbook.json").read_text())

    book = parse_polymarket_book("token-yes", "yes", payload)

    assert book.sequence == "book-hash-1"
    assert book.asks[0].price == Decimal("0.11")

