from datetime import UTC, datetime
from typing import Any

from backend.app.domain.market import NormalizedBook, normalize_levels


def parse_polymarket_book(
    market_id: str,
    outcome: str,
    payload: dict[str, Any],
    received_at: datetime | None = None,
) -> NormalizedBook:
    captured_at = datetime.fromtimestamp(int(payload["timestamp"]) / 1000, tz=UTC)
    levels = normalize_levels(
        (level["price"], level["size"]) for level in payload.get("asks", [])
    )
    return NormalizedBook(
        market_id=market_id,
        outcome=outcome.lower(),
        sequence=str(payload["hash"]),
        captured_at=captured_at,
        received_at=received_at or datetime.now(UTC),
        asks=tuple(levels),
    )

