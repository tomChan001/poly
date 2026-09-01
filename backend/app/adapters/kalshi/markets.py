from decimal import Decimal
from typing import Any

from backend.app.core.decimal import parse_decimal, parse_price
from backend.app.domain.enums import Venue
from backend.app.domain.metadata import MarketMetadata


def normalize_kalshi_market(payload: dict[str, Any]) -> MarketMetadata:
    ticker = _required_text(payload, "ticker")
    rules_primary = _required_text(payload, "rules_primary")
    rules_url_value = payload.get("rules_url")
    rules_url = (
        rules_url_value.strip()
        if isinstance(rules_url_value, str) and rules_url_value.strip()
        else f"https://kalshi.com/markets/{ticker}"
    )

    return MarketMetadata(
        venue=Venue.KALSHI,
        external_id=ticker,
        title=str(payload["title"]),
        status=str(payload["status"]),
        outcomes=("yes", "no"),
        rule_text=rules_primary,
        rule_url=rules_url,
        minimum_tick=_minimum_tick(payload),
        minimum_quantity=_minimum_quantity(payload),
        raw_payload=payload,
    )


def _minimum_tick(payload: dict[str, Any]) -> Decimal:
    legacy_tick = payload.get("tick_size")
    if legacy_tick is not None:
        minimum_tick = parse_price(legacy_tick)
        if minimum_tick <= 0:
            raise ValueError("Kalshi tick_size must be positive")
        return minimum_tick

    price_ranges = payload.get("price_ranges")
    if not isinstance(price_ranges, list):
        raise TypeError("Kalshi price_ranges must be a list")
    if not price_ranges:
        raise ValueError("Kalshi price_ranges must not be empty")

    steps: list[Decimal] = []
    for price_range in price_ranges:
        if not isinstance(price_range, dict):
            raise TypeError("Kalshi price_ranges entries must be mappings")
        if not all(name in price_range for name in ("start", "end", "step")):
            raise TypeError("Kalshi price_ranges entries require start, end, and step")

        start = parse_price(price_range["start"])
        end = parse_price(price_range["end"])
        step = parse_price(price_range["step"])
        if start >= end:
            raise ValueError("Kalshi price range start must be less than end")
        if step <= 0:
            raise ValueError("Kalshi price range step must be positive")
        if step > end - start:
            raise ValueError("Kalshi price range step must not exceed its width")
        steps.append(step)

    return min(steps)


def _minimum_quantity(payload: dict[str, Any]) -> Decimal:
    if "minimum_order_size" not in payload:
        return Decimal(1)

    minimum_quantity = parse_decimal(payload["minimum_order_size"])
    if minimum_quantity <= 0:
        raise ValueError("Kalshi minimum_order_size must be positive")
    return minimum_quantity


def _required_text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Kalshi {name} must be non-empty text")
    return value.strip()

