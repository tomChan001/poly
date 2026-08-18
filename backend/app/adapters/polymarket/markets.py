from typing import Any

from backend.app.core.decimal import parse_decimal, parse_price
from backend.app.domain.enums import Venue
from backend.app.domain.metadata import MarketMetadata


def normalize_polymarket_market(payload: dict[str, Any]) -> MarketMetadata:
    return MarketMetadata(
        venue=Venue.POLYMARKET,
        external_id=str(payload["condition_id"]),
        title=str(payload["question"]),
        status="open" if payload.get("active") else "closed",
        outcomes=("yes", "no"),
        rule_text=str(payload["description"]),
        rule_url=str(payload["url"]),
        minimum_tick=parse_price(str(payload["minimum_tick_size"])),
        minimum_quantity=parse_decimal(str(payload["minimum_order_size"])),
        raw_payload=payload,
    )

