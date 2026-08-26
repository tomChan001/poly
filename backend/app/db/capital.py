from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.domain.enums import Venue
from backend.app.services.capital import (
    CapitalLedger,
    CapitalReservation,
    InsufficientCapital,
    PairReservation,
)


class PostgresCapitalLedger(CapitalLedger):
    """Atomic, restart-safe reservation ledger backed by capital_reservation."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__({})
        self._sessions = sessions

    async def refresh(self) -> None:
        async with self._sessions() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, correlation_id, event_id, venue, principal, status
                    FROM capital_reservation
                    WHERE status IN ('active', 'consumed')
                    ORDER BY created_at, venue
                    """
                )
            )
            rows = list(result.mappings())
        self._restore(rows)

    async def reserve_pair(
        self,
        correlation_id: str,
        kalshi_amount: Decimal,
        polymarket_amount: Decimal,
        *,
        event_id: str | None = None,
    ) -> PairReservation:
        normalized_event_id = event_id or correlation_id
        async with self._lock, self._sessions.begin() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('poly-capital-reservation'))")
            )
            existing_result = await session.execute(
                text(
                    """
                    SELECT id, correlation_id, event_id, venue, principal, status
                    FROM capital_reservation
                    WHERE correlation_id = :correlation_id
                    ORDER BY venue
                    """
                ),
                {"correlation_id": correlation_id},
            )
            existing_rows = list(existing_result.mappings())
            if existing_rows:
                pair = _pair_from_rows(existing_rows)
                statuses = {str(row["status"]) for row in existing_rows}
                if statuses == {"released"}:
                    raise ValueError("capital reservation is released")
                if len(statuses) != 1 or not statuses <= {"active", "consumed"}:
                    raise RuntimeError("capital reservation pair has inconsistent status")
                if (
                    pair.event_id != normalized_event_id
                    or pair.kalshi.amount != kalshi_amount
                    or pair.polymarket.amount != polymarket_amount
                ):
                    raise ValueError("capital reservation conflict")
                self._cache_pair(pair, statuses.pop())
                return pair

            active_result = await session.execute(
                text(
                    """
                    SELECT venue, COALESCE(SUM(principal + fee_buffer + hedge_buffer), 0)
                        AS reserved
                    FROM capital_reservation
                    WHERE status = 'active'
                    GROUP BY venue
                    """
                )
            )
            active = {
                Venue(row["venue"]): Decimal(row["reserved"])
                for row in active_result.mappings()
            }
            requested = {
                Venue.KALSHI: kalshi_amount,
                Venue.POLYMARKET: polymarket_amount,
            }
            for venue, amount in requested.items():
                account = self._accounts.get(venue)
                usable = Decimal(0) if account is None else account.usable
                if usable - active.get(venue, Decimal(0)) < amount:
                    raise InsufficientCapital(f"insufficient capital on {venue}")

            now = datetime.now(UTC)
            pair = PairReservation(
                correlation_id=correlation_id,
                kalshi=CapitalReservation(
                    correlation_id,
                    Venue.KALSHI,
                    kalshi_amount,
                    str(uuid4()),
                ),
                polymarket=CapitalReservation(
                    correlation_id,
                    Venue.POLYMARKET,
                    polymarket_amount,
                    str(uuid4()),
                ),
                event_id=normalized_event_id,
            )
            for reservation in (pair.kalshi, pair.polymarket):
                await session.execute(
                    text(
                        """
                        INSERT INTO capital_reservation (
                            id, created_at, execution_id, correlation_id, event_id,
                            venue, principal, fee_buffer, hedge_buffer, expires_at, status
                        ) VALUES (
                            :id, :created_at, NULL, :correlation_id, :event_id,
                            :venue, :principal, 0, 0, :expires_at, 'active'
                        )
                        """
                    ),
                    {
                        "id": UUID(reservation.reservation_id),
                        "created_at": now,
                        "correlation_id": correlation_id,
                        "event_id": normalized_event_id,
                        "venue": reservation.venue.value,
                        "principal": reservation.amount,
                        "expires_at": now + timedelta(days=365),
                    },
                )
            self._cache_pair(pair, "active")
            return pair

    async def release_pair(self, correlation_id: str) -> PairReservation | None:
        return await self._transition(correlation_id, "released")

    async def convert_pair(self, correlation_id: str) -> PairReservation | None:
        return await self._transition(correlation_id, "consumed")

    async def _transition(
        self,
        correlation_id: str,
        target: str,
    ) -> PairReservation | None:
        async with self._lock, self._sessions.begin() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE capital_reservation
                    SET status = :target
                    WHERE correlation_id = :correlation_id
                      AND status IN ('active', :target)
                    RETURNING id, correlation_id, event_id, venue, principal, status
                    """
                ),
                {"target": target, "correlation_id": correlation_id},
            )
            rows = list(result.mappings())
            if not rows:
                return None
            pair = _pair_from_rows(rows)
            self._pairs.pop(correlation_id, None)
            for venue in Venue:
                self.reservations.pop((correlation_id, venue), None)
            if target == "consumed":
                self.consumed_pairs[correlation_id] = pair
            return pair

    def _restore(self, rows: Sequence[RowMapping]) -> None:
        self.reservations.clear()
        self._pairs.clear()
        self.consumed_pairs.clear()
        grouped: dict[str, list[RowMapping]] = {}
        for row in rows:
            correlation_id = str(row["correlation_id"])
            grouped.setdefault(correlation_id, []).append(row)
        for pair_rows in grouped.values():
            pair = _pair_from_rows(pair_rows)
            self._cache_pair(pair, str(pair_rows[0]["status"]))

    def _cache_pair(self, pair: PairReservation, status: str) -> None:
        if status == "consumed":
            self.consumed_pairs[pair.correlation_id] = pair
            return
        if status != "active":
            raise RuntimeError(f"cannot cache capital reservation with status {status}")
        self._pairs[pair.correlation_id] = pair
        self.reservations[(pair.correlation_id, Venue.KALSHI)] = pair.kalshi
        self.reservations[(pair.correlation_id, Venue.POLYMARKET)] = pair.polymarket


def _pair_from_rows(rows: Sequence[RowMapping]) -> PairReservation:
    if len(rows) != 2:
        raise RuntimeError("capital reservation pair is incomplete")
    by_venue = {Venue(row["venue"]): row for row in rows}
    if set(by_venue) != set(Venue):
        raise RuntimeError("capital reservation pair has invalid venues")

    def reservation(venue: Venue) -> CapitalReservation:
        row = by_venue[venue]
        return CapitalReservation(
            correlation_id=str(row["correlation_id"]),
            venue=venue,
            amount=Decimal(row["principal"]),
            reservation_id=str(row["id"]),
        )

    first = rows[0]
    return PairReservation(
        correlation_id=str(first["correlation_id"]),
        kalshi=reservation(Venue.KALSHI),
        polymarket=reservation(Venue.POLYMARKET),
        event_id=str(first["event_id"] or first["correlation_id"]),
    )
