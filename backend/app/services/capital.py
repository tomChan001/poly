import asyncio
from dataclasses import dataclass
from decimal import Decimal

from backend.app.domain.enums import Venue


class InsufficientCapital(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AccountCapital:
    venue: Venue
    platform_available: Decimal
    open_order_reserve: Decimal
    hedge_buffer: Decimal
    fee_buffer: Decimal
    unsettled_capital: Decimal

    @property
    def usable(self) -> Decimal:
        return max(
            Decimal(0),
            self.platform_available
            - self.open_order_reserve
            - self.hedge_buffer
            - self.fee_buffer
            - self.unsettled_capital,
        )


@dataclass(frozen=True, slots=True)
class CapitalReservation:
    correlation_id: str
    venue: Venue
    amount: Decimal


@dataclass(frozen=True, slots=True)
class PairReservation:
    correlation_id: str
    kalshi: CapitalReservation
    polymarket: CapitalReservation


class CapitalLedger:
    def __init__(self, accounts: dict[Venue, AccountCapital]) -> None:
        self._accounts = dict(accounts)
        self._lock = asyncio.Lock()
        self.reservations: dict[tuple[str, Venue], CapitalReservation] = {}
        self._pairs: dict[str, PairReservation] = {}

    async def reserve_pair(
        self,
        correlation_id: str,
        kalshi_amount: Decimal,
        polymarket_amount: Decimal,
    ) -> PairReservation:
        async with self._lock:
            existing = self._pairs.get(correlation_id)
            if existing is not None:
                return existing

            requested = {
                Venue.KALSHI: kalshi_amount,
                Venue.POLYMARKET: polymarket_amount,
            }
            for venue, amount in requested.items():
                already_reserved = sum(
                    reservation.amount
                    for (_, reserved_venue), reservation in self.reservations.items()
                    if reserved_venue is venue
                )
                if self._accounts[venue].usable - already_reserved < amount:
                    raise InsufficientCapital(f"insufficient capital on {venue}")

            kalshi = CapitalReservation(correlation_id, Venue.KALSHI, kalshi_amount)
            polymarket = CapitalReservation(
                correlation_id,
                Venue.POLYMARKET,
                polymarket_amount,
            )
            pair = PairReservation(correlation_id, kalshi, polymarket)
            self.reservations[(correlation_id, Venue.KALSHI)] = kalshi
            self.reservations[(correlation_id, Venue.POLYMARKET)] = polymarket
            self._pairs[correlation_id] = pair
            return pair

