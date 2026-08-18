from decimal import Decimal

import pytest

from backend.app.domain.enums import Venue
from backend.app.services.capital import (
    AccountCapital,
    CapitalLedger,
    InsufficientCapital,
)


def account(venue: Venue, available: str) -> AccountCapital:
    return AccountCapital(
        venue=venue,
        platform_available=Decimal(available),
        open_order_reserve=Decimal(0),
        hedge_buffer=Decimal(2),
        fee_buffer=Decimal(1),
        unsettled_capital=Decimal(0),
    )


@pytest.mark.asyncio
async def test_pair_reservation_is_atomic_when_one_venue_lacks_capital() -> None:
    ledger = CapitalLedger(
        {
            Venue.KALSHI: account(Venue.KALSHI, "100"),
            Venue.POLYMARKET: account(Venue.POLYMARKET, "5"),
        }
    )

    with pytest.raises(InsufficientCapital):
        await ledger.reserve_pair("corr-1", Decimal(10), Decimal(10))

    assert ledger.reservations == {}


@pytest.mark.asyncio
async def test_pair_reservation_is_idempotent_by_correlation_id() -> None:
    ledger = CapitalLedger(
        {
            Venue.KALSHI: account(Venue.KALSHI, "100"),
            Venue.POLYMARKET: account(Venue.POLYMARKET, "100"),
        }
    )

    first = await ledger.reserve_pair("corr-1", Decimal(10), Decimal(8))
    second = await ledger.reserve_pair("corr-1", Decimal(10), Decimal(8))

    assert first is second
    assert len(ledger.reservations) == 2

