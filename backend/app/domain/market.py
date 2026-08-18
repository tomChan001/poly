from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from backend.app.core.decimal import DecimalInput, parse_decimal, parse_price


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class NormalizedBook:
    market_id: str
    outcome: str
    sequence: str
    captured_at: datetime
    received_at: datetime
    asks: tuple[BookLevel, ...]


def normalize_levels(
    raw_levels: Iterable[tuple[DecimalInput, DecimalInput]],
) -> list[BookLevel]:
    merged: dict[Decimal, Decimal] = {}
    for raw_price, raw_quantity in raw_levels:
        price = parse_price(raw_price)
        quantity = parse_decimal(raw_quantity)
        if quantity == 0:
            continue
        merged[price] = merged.get(price, Decimal(0)) + quantity

    return [BookLevel(price, merged[price]) for price in sorted(merged)]


def kalshi_asks_from_opposite_bids(bids: Iterable[BookLevel]) -> list[BookLevel]:
    """Convert Kalshi's opposite-outcome bids into executable ask prices.

    Kalshi exposes YES and NO bid ladders. Buying NO at 0.76 therefore consumes
    a YES bid at 0.24; treating the 0.24 bid as a NO ask would overstate ROI.
    """
    asks = [BookLevel(Decimal(1) - bid.price, bid.quantity) for bid in bids]
    return sorted(asks, key=lambda level: level.price)

