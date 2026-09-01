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
        minimum_tick=parse_price(str(payload["tick_size"])),
        minimum_quantity=parse_decimal(str(payload["minimum_order_size"])),
        raw_payload=payload,
    )


def _required_text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Kalshi {name} must be non-empty text")
    return value.strip()

