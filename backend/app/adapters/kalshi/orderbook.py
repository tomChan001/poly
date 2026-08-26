from datetime import UTC, datetime
from typing import Any

from backend.app.domain.market import (
    NormalizedBook,
    kalshi_asks_from_opposite_bids,
    normalize_levels,
)


def parse_kalshi_book(
    market_id: str,
    outcome: str,
    payload: dict[str, Any],
    captured_at: datetime | None,
    received_at: datetime | None = None,
) -> NormalizedBook:
    normalized_outcome = outcome.lower()
    if normalized_outcome not in {"yes", "no"}:
        raise ValueError("Kalshi outcome must be yes or no")

    # Kalshi exposes bids for both outcomes. To buy one outcome, consume the
    # opposite outcome's bids and complement each price around $1.
    opposite_side = "no" if normalized_outcome == "yes" else "yes"
    opposite_bids = normalize_levels(payload.get(opposite_side, []))
    asks = kalshi_asks_from_opposite_bids(opposite_bids)
    return NormalizedBook(
        market_id=market_id,
        outcome=normalized_outcome,
        sequence=str(payload["sequence"]),
        captured_at=captured_at,
        received_at=received_at or datetime.now(UTC),
        asks=tuple(asks),
    )
