"""Market constraints shared by read-only review and execution."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.app.domain.market import NormalizedBook
from backend.app.services.executable_pairs import ExecutablePair
from backend.app.services.settings import RiskPolicy


def settlement_reasons(
    pair: ExecutablePair, policy: RiskPolicy, now: datetime
) -> tuple[str, ...]:
    dates = (pair.kalshi_expected_settlement_at, pair.polymarket_expected_settlement_at)
    if any(value is None for value in dates):
        return ("SETTLEMENT_UNKNOWN",)
    known = [
        value.replace(tzinfo=UTC) if value.tzinfo is None else value
        for value in dates
        if value is not None
    ]
    if min(known) <= now:
        return ("SETTLEMENT_PASSED",)
    if pair.worst_case_settlement_at is not None:
        horizon = pair.worst_case_settlement_at
        known.append(horizon.replace(tzinfo=UTC) if horizon.tzinfo is None else horizon)
    if max(known) > now + timedelta(days=policy.maximum_settlement_days):
        return ("SETTLEMENT_TOO_LATE",)
    return ()


def paired_liquidity(kalshi: NormalizedBook, polymarket: NormalizedBook) -> Decimal:
    """Count shares available at the best ask, never distant book volume."""
    return min(best_ask_quantity(kalshi), best_ask_quantity(polymarket))


def best_ask_quantity(book: NormalizedBook) -> Decimal:
    if not book.asks:
        return Decimal(0)
    best = min(level.price for level in book.asks)
    return sum(
        (level.quantity for level in book.asks if level.price == best), Decimal(0)
    )


def liquidity_reasons(
    kalshi: NormalizedBook,
    polymarket: NormalizedBook,
    policy: RiskPolicy,
) -> tuple[str, ...]:
    if paired_liquidity(kalshi, polymarket) < policy.minimum_liquidity_contracts:
        return ("INSUFFICIENT_LIQUIDITY",)
    return ()
