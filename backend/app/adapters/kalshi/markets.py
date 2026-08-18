from typing import Any

from backend.app.core.decimal import parse_decimal, parse_price
from backend.app.domain.enums import Venue
from backend.app.domain.metadata import MarketMetadata


def normalize_kalshi_market(payload: dict[str, Any]) -> MarketMetadata:
    return MarketMetadata(
        venue=Venue.KALSHI,
        external_id=str(payload["ticker"]),
        title=str(payload["title"]),
        status=str(payload["status"]),
        outcomes=("yes", "no"),
        rule_text=str(payload["rules_primary"]),
        rule_url=str(payload["rules_url"]),
        minimum_tick=parse_price(str(payload["tick_size"])),
        minimum_quantity=parse_decimal(str(payload["minimum_order_size"])),
        raw_payload=payload,
    )

