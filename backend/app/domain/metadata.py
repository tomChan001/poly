from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from backend.app.domain.enums import Venue


@dataclass(frozen=True, slots=True)
class MarketMetadata:
    venue: Venue
    external_id: str
    title: str
    status: str
    outcomes: tuple[str, ...]
    rule_text: str
    rule_url: str
    minimum_tick: Decimal
    minimum_quantity: Decimal
    raw_payload: dict[str, Any]

