import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.domain.enums import ExecutionState, Venue
from backend.app.services.execution import (
    ExecutionEvidence,
    ExecutionRecord,
    FillReport,
    OrderStatus,
    OrderSubmissionResult,
    StateTransition,
)


class PostgresExecutionStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(self, record: ExecutionRecord) -> None:
        occurred_at = min(
            (transition.occurred_at for transition in record.transitions),
            default=datetime.now(UTC),
        )
        async with self._sessions.begin() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO execution_record (
                        correlation_id, occurred_at, state, snapshot, updated_at
                    ) VALUES (
                        :correlation_id, :occurred_at, :state,
                        CAST(:snapshot AS jsonb), now()
                    )
                    ON CONFLICT (correlation_id) DO UPDATE SET
                        occurred_at = LEAST(execution_record.occurred_at, EXCLUDED.occurred_at),
                        state = EXCLUDED.state,
                        snapshot = EXCLUDED.snapshot,
                        updated_at = now()
                    """
                ),
                {
                    "correlation_id": record.correlation_id,
                    "occurred_at": occurred_at,
                    "state": record.state.value,
                    "snapshot": json.dumps(_snapshot(record)),
                },
            )

    async def list(self) -> list[ExecutionRecord]:
        async with self._sessions() as session:
            result = await session.execute(
                text("SELECT snapshot FROM execution_record ORDER BY occurred_at DESC")
            )
            return [_record(row._mapping["snapshot"]) for row in result]

    async def get(self, correlation_id: str) -> ExecutionRecord:
        async with self._sessions() as session:
            result = await session.execute(
                text(
                    "SELECT snapshot FROM execution_record WHERE correlation_id = :correlation_id"
                ),
                {"correlation_id": correlation_id},
            )
            row = result.first()
            if row is None:
                raise KeyError(correlation_id)
            return _record(row._mapping["snapshot"])


def _snapshot(record: ExecutionRecord) -> dict[str, object]:
    return {
        "correlation_id": record.correlation_id,
        "state": record.state.value,
        "requested_quantity": str(record.requested_quantity),
        "matched_quantity": str(record.matched_quantity),
        "unhedged_quantity": str(record.unhedged_quantity),
        "evidence": _evidence_snapshot(record.evidence),
        "legs": {
            venue.value: {
                "client_order_id": leg.client_order_id,
                "status": leg.status.value,
                "fills": [
                    {
                        "fill_id": fill.fill_id,
                        "quantity": str(fill.quantity),
                        "price": str(fill.price),
                        "fee": str(fill.fee),
                    }
                    for fill in leg.fills
                ],
            }
            for venue, leg in record.legs.items()
        },
        "transitions": [
            {
                "source": transition.source.value,
                "target": transition.target.value,
                "occurred_at": transition.occurred_at.isoformat(),
            }
            for transition in record.transitions
        ],
    }


def _record(snapshot: dict[str, object]) -> ExecutionRecord:
    raw_legs = snapshot.get("legs")
    raw_transitions = snapshot.get("transitions")
    if not isinstance(raw_legs, dict) or not isinstance(raw_transitions, list):
        raise TypeError("execution snapshot has invalid collections")
    legs: dict[Venue, OrderSubmissionResult] = {}
    for venue_name, raw_leg in raw_legs.items():
        if not isinstance(venue_name, str) or not isinstance(raw_leg, dict):
            raise TypeError("execution leg snapshot is invalid")
        raw_fills = raw_leg.get("fills")
        if not isinstance(raw_fills, list):
            raise TypeError("execution fills snapshot is invalid")
        fills = tuple(
            FillReport(
                str(raw_fill["fill_id"]),
                Decimal(str(raw_fill["quantity"])),
                Decimal(str(raw_fill["price"])),
                Decimal(str(raw_fill["fee"])),
            )
            for raw_fill in raw_fills
            if isinstance(raw_fill, dict)
        )
        legs[Venue(venue_name)] = OrderSubmissionResult(
            str(raw_leg["client_order_id"]),
            OrderStatus(str(raw_leg["status"])),
            fills,
        )
    transitions = [
        StateTransition(
            ExecutionState(str(raw_transition["source"])),
            ExecutionState(str(raw_transition["target"])),
            datetime.fromisoformat(str(raw_transition["occurred_at"])),
        )
        for raw_transition in raw_transitions
        if isinstance(raw_transition, dict)
    ]
    return ExecutionRecord(
        correlation_id=str(snapshot["correlation_id"]),
        state=ExecutionState(str(snapshot["state"])),
        requested_quantity=Decimal(str(snapshot["requested_quantity"])),
        evidence=_restore_evidence(snapshot.get("evidence")),
        legs=legs,
        matched_quantity=Decimal(str(snapshot["matched_quantity"])),
        unhedged_quantity=Decimal(str(snapshot["unhedged_quantity"])),
        transitions=transitions,
    )


def _evidence_snapshot(evidence: ExecutionEvidence | None) -> dict[str, object] | None:
    if evidence is None:
        return None
    return {
        "quote_evaluation_id": evidence.quote_evaluation_id,
        "rule_versions": list(evidence.rule_versions),
        "book_sequences": list(evidence.book_sequences),
        "balance_versions": list(evidence.balance_versions),
        "risk_policy_version": evidence.risk_policy_version,
        "capital_reservation_id": evidence.capital_reservation_id,
        "quantity": str(evidence.quantity),
        "kalshi_market_id": evidence.kalshi_market_id,
        "polymarket_market_id": evidence.polymarket_market_id,
        "kalshi_outcome": evidence.kalshi_outcome,
        "polymarket_outcome": evidence.polymarket_outcome,
        "kalshi_limit_price": str(evidence.kalshi_limit_price),
        "polymarket_limit_price": str(evidence.polymarket_limit_price),
        "conservative_roi": str(evidence.conservative_roi),
        "minimum_roi": str(evidence.minimum_roi),
        "estimated_fees": [str(value) for value in evidence.estimated_fees],
    }


def _restore_evidence(raw: object) -> ExecutionEvidence | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError("execution evidence snapshot is invalid")
    return ExecutionEvidence(
        quote_evaluation_id=str(raw["quote_evaluation_id"]),
        rule_versions=_string_pair(raw["rule_versions"]),
        book_sequences=_string_pair(raw["book_sequences"]),
        balance_versions=_string_pair(raw["balance_versions"]),
        risk_policy_version=str(raw["risk_policy_version"]),
        capital_reservation_id=str(raw["capital_reservation_id"]),
        quantity=Decimal(str(raw["quantity"])),
        kalshi_market_id=str(raw["kalshi_market_id"]),
        polymarket_market_id=str(raw["polymarket_market_id"]),
        kalshi_outcome=str(raw["kalshi_outcome"]),
        polymarket_outcome=str(raw["polymarket_outcome"]),
        kalshi_limit_price=Decimal(str(raw["kalshi_limit_price"])),
        polymarket_limit_price=Decimal(str(raw["polymarket_limit_price"])),
        conservative_roi=Decimal(str(raw["conservative_roi"])),
        minimum_roi=Decimal(str(raw["minimum_roi"])),
        estimated_fees=_decimal_pair(raw.get("estimated_fees", ["0", "0"])),
    )


def _string_pair(raw: object) -> tuple[str, str]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise TypeError("execution evidence pair is invalid")
    return str(raw[0]), str(raw[1])


def _decimal_pair(raw: object) -> tuple[Decimal, Decimal]:
    left, right = _string_pair(raw)
    return Decimal(left), Decimal(right)
