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


@pytest.mark.asyncio
async def test_pair_reservation_rejects_same_id_with_different_amounts() -> None:
    ledger = CapitalLedger(
        {
            Venue.KALSHI: account(Venue.KALSHI, "100"),
            Venue.POLYMARKET: account(Venue.POLYMARKET, "100"),
        }
    )
    await ledger.reserve_pair("corr-1", Decimal(10), Decimal(8))

    with pytest.raises(ValueError, match="reservation conflict"):
        await ledger.reserve_pair("corr-1", Decimal(11), Decimal(8))


@pytest.mark.asyncio
async def test_releasing_pair_frees_both_venue_reservations() -> None:
    ledger = CapitalLedger(
        {
            Venue.KALSHI: account(Venue.KALSHI, "100"),
            Venue.POLYMARKET: account(Venue.POLYMARKET, "100"),
        }
    )

    await ledger.reserve_pair("corr-1", Decimal(10), Decimal(8))
    await ledger.release_pair("corr-1")

    assert ledger.reservations == {}


@pytest.mark.asyncio
async def test_converting_pair_closes_active_reservations() -> None:
    ledger = CapitalLedger(
        {
            Venue.KALSHI: account(Venue.KALSHI, "100"),
            Venue.POLYMARKET: account(Venue.POLYMARKET, "100"),
        }
    )

    pair = await ledger.reserve_pair("corr-1", Decimal(10), Decimal(8))
    converted = await ledger.convert_pair("corr-1")

    assert converted is pair
    assert ledger.reservations == {}
    assert ledger.consumed_pairs["corr-1"] is pair


@pytest.mark.asyncio
async def test_event_and_portfolio_exposure_include_consumed_reservations() -> None:
    ledger = CapitalLedger(
        {
            Venue.KALSHI: account(Venue.KALSHI, "100"),
            Venue.POLYMARKET: account(Venue.POLYMARKET, "100"),
        }
    )
    await ledger.reserve_pair(
        "corr-1",
        Decimal(10),
        Decimal(8),
        event_id="event-1",
    )
    await ledger.convert_pair("corr-1")

    assert ledger.event_exposure("event-1") == Decimal(18)
    assert ledger.portfolio_exposure() == Decimal(18)
    assert ledger.remaining_event_limit("event-1", Decimal(25)) == Decimal(7)
    assert ledger.remaining_portfolio_limit(Decimal(100)) == Decimal(82)
