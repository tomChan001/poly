import asyncio
from dataclasses import dataclass, field, replace
from decimal import Decimal
from uuid import uuid4

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
    reservation_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True, slots=True)
class PairReservation:
    correlation_id: str
    kalshi: CapitalReservation
    polymarket: CapitalReservation
    event_id: str

    @property
    def amount(self) -> Decimal:
        return self.kalshi.amount + self.polymarket.amount

    @property
    def evidence_id(self) -> str:
        return f"{self.kalshi.reservation_id}:{self.polymarket.reservation_id}"

class CapitalLedger:
    def __init__(self, accounts: dict[Venue, AccountCapital]) -> None:
        self._accounts = dict(accounts)
        self._lock = asyncio.Lock()
        self.reservations: dict[tuple[str, Venue], CapitalReservation] = {}
        self._pairs: dict[str, PairReservation] = {}
        self.consumed_pairs: dict[str, PairReservation] = {}

    async def refresh(self) -> None:
        """Reload durable reservation state when implemented by a database ledger."""

    def sync_available_balances(
        self,
        *,
        kalshi_available: Decimal,
        polymarket_available: Decimal,
    ) -> None:
        self._sync_account(Venue.KALSHI, kalshi_available)
        self._sync_account(Venue.POLYMARKET, polymarket_available)

    def available(self, venue: Venue) -> Decimal:
        account = self._accounts.get(venue)
        if account is None:
            return Decimal(0)
        already_reserved = sum(
            reservation.amount
            for (_, reserved_venue), reservation in self.reservations.items()
            if reserved_venue is venue
        )
        return max(Decimal(0), account.usable - already_reserved)

    async def reserve_pair(
        self,
        correlation_id: str,
        kalshi_amount: Decimal,
        polymarket_amount: Decimal,
        *,
        event_id: str | None = None,
    ) -> PairReservation:
        async with self._lock:
            existing = self._pairs.get(correlation_id)
            if existing is not None:
                if (
                    existing.kalshi.amount != kalshi_amount
                    or existing.polymarket.amount != polymarket_amount
                    or existing.event_id != (event_id or correlation_id)
                ):
                    raise ValueError("capital reservation conflict")
                return existing

            requested = {
                Venue.KALSHI: kalshi_amount,
                Venue.POLYMARKET: polymarket_amount,
            }
            for venue, amount in requested.items():
                if self.available(venue) < amount:
                    raise InsufficientCapital(f"insufficient capital on {venue}")

            kalshi = CapitalReservation(correlation_id, Venue.KALSHI, kalshi_amount)
            polymarket = CapitalReservation(
                correlation_id,
                Venue.POLYMARKET,
                polymarket_amount,
            )
            pair = PairReservation(
                correlation_id,
                kalshi,
                polymarket,
                event_id or correlation_id,
            )
            self.reservations[(correlation_id, Venue.KALSHI)] = kalshi
            self.reservations[(correlation_id, Venue.POLYMARKET)] = polymarket
            self._pairs[correlation_id] = pair
            return pair

    async def release_pair(self, correlation_id: str) -> PairReservation | None:
        async with self._lock:
            pair = self._pairs.pop(correlation_id, None)
            if pair is None:
                return None
            self.reservations.pop((correlation_id, Venue.KALSHI), None)
            self.reservations.pop((correlation_id, Venue.POLYMARKET), None)
            return pair

    async def convert_pair(self, correlation_id: str) -> PairReservation | None:
        async with self._lock:
            pair = self._pairs.pop(correlation_id, None)
            if pair is None:
                return self.consumed_pairs.get(correlation_id)
            self.reservations.pop((correlation_id, Venue.KALSHI), None)
            self.reservations.pop((correlation_id, Venue.POLYMARKET), None)
            self.consumed_pairs[correlation_id] = pair
            return pair

    def event_exposure(self, event_id: str) -> Decimal:
        return sum(
            (
                pair.amount
                for pair in (*self._pairs.values(), *self.consumed_pairs.values())
                if pair.event_id == event_id
            ),
            Decimal(0),
        )

    def portfolio_exposure(self) -> Decimal:
        return sum(
            (
                pair.amount
                for pair in (*self._pairs.values(), *self.consumed_pairs.values())
            ),
            Decimal(0),
        )

    def get_pair(self, correlation_id: str) -> PairReservation | None:
        return self._pairs.get(correlation_id) or self.consumed_pairs.get(correlation_id)

    def remaining_event_limit(self, event_id: str, limit: Decimal) -> Decimal:
        return max(Decimal(0), limit - self.event_exposure(event_id))

    def remaining_portfolio_limit(self, limit: Decimal) -> Decimal:
        return max(Decimal(0), limit - self.portfolio_exposure())

    def _sync_account(self, venue: Venue, available: Decimal) -> None:
        current = self._accounts.get(venue)
        if current is None:
            self._accounts[venue] = AccountCapital(
                venue=venue,
                platform_available=available,
                open_order_reserve=Decimal(0),
                hedge_buffer=Decimal(0),
                fee_buffer=Decimal(0),
                unsettled_capital=Decimal(0),
            )
            return
        self._accounts[venue] = replace(current, platform_available=available)
