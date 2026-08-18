from decimal import Decimal

import pytest

from backend.app.core.decimal import parse_decimal, parse_price
from backend.app.domain.market import (
    BookLevel,
    kalshi_asks_from_opposite_bids,
    normalize_levels,
)


def test_kalshi_ask_is_one_minus_opposite_bid() -> None:
    result = kalshi_asks_from_opposite_bids(
        [BookLevel(price=Decimal("0.24"), quantity=Decimal(505))]
    )

    assert result == [BookLevel(price=Decimal("0.76"), quantity=Decimal(505))]


def test_decimal_parser_rejects_binary_float() -> None:
    with pytest.raises(TypeError, match="float"):
        parse_decimal(0.1)


def test_price_parser_rejects_out_of_range_value() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        parse_price("1.01")


def test_levels_are_merged_sorted_and_zero_quantity_removed() -> None:
    levels = normalize_levels(
        [
            ("0.60", "2"),
            ("0.40", "1"),
            ("0.60", "3"),
            ("0.50", "0"),
        ]
    )

    assert levels == [
        BookLevel(price=Decimal("0.40"), quantity=Decimal(1)),
        BookLevel(price=Decimal("0.60"), quantity=Decimal(5)),
    ]

