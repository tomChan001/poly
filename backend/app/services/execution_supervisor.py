from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from backend.app.core.config import TradingMode
from backend.app.domain.enums import ExecutionState, Venue
from backend.app.services.emergency_hedge import EmergencyHedgeService, UnhedgedExposure
from backend.app.services.execution import (
    ExecutionEvidence,
    ExecutionRecord,
    ExecutionTradingPort,
)
from backend.app.services.fees import FeeReconciliationService
from backend.app.services.notifications import NotificationService
from backend.app.services.system_control import SystemControl


@dataclass(frozen=True, slots=True)
class ExecutionIncident:
    idempotency_key: str
    correlation_id: str
    state: ExecutionState
    action: str
    simulated: bool
    unhedged_quantity: str
    occurred_at: datetime


class IncidentStore(Protocol):
    async def get(self, idempotency_key: str) -> ExecutionIncident | None: ...

    async def add_if_absent(self, incident: ExecutionIncident) -> ExecutionIncident: ...

    async def claim(
        self,
        incident: ExecutionIncident,
    ) -> tuple[ExecutionIncident, bool]: ...


class InMemoryIncidentStore:
    def __init__(self) -> None:
        self.records: dict[str, ExecutionIncident] = {}

    async def get(self, idempotency_key: str) -> ExecutionIncident | None:
        return self.records.get(idempotency_key)

    async def add_if_absent(self, incident: ExecutionIncident) -> ExecutionIncident:
        return self.records.setdefault(incident.idempotency_key, incident)

    async def claim(
        self,
        incident: ExecutionIncident,
    ) -> tuple[ExecutionIncident, bool]:
        existing = self.records.get(incident.idempotency_key)
        if existing is not None:
            return existing, False
        self.records[incident.idempotency_key] = incident
        return incident, True

    async def list(self) -> list[ExecutionIncident]:
        return list(self.records.values())


class ExecutionSupervisor:
    def __init__(
        self,
        system_control: SystemControl,
        incidents: IncidentStore,
        notifications: NotificationService,
        *,
        emergency_service_factory: Callable[[TradingMode], EmergencyHedgeService]
        | None = None,
        fee_reconciliation: FeeReconciliationService | None = None,
    ) -> None:
        self._system_control = system_control
        self._incidents = incidents
        self._notifications = notifications
        self._emergency_service_factory = emergency_service_factory
        self._fee_reconciliation = fee_reconciliation or FeeReconciliationService(
            system_control
        )

    def bind_emergency_ports(
        self,
        ports: Mapping[Venue, ExecutionTradingPort],
    ) -> None:
        bound_ports = dict(ports)
        self._emergency_service_factory = lambda mode: EmergencyHedgeService(
            bound_ports,
            trading_mode=mode,
        )

    async def finalize(
        self,
        record: ExecutionRecord,
        evidence: ExecutionEvidence | None = None,
        *,
        mode: TradingMode,
        now: datetime | None = None,
        occurred_at: datetime | None = None,
        maximum_unhedged_loss: Decimal = Decimal(0),
    ) -> ExecutionIncident | None:
        if evidence is not None and record.state is ExecutionState.PAIRED:
            estimated = sum(evidence.estimated_fees, Decimal(0))
            actual = sum(
                (
                    fill.fee
                    for leg in record.legs.values()
                    for fill in {item.fill_id: item for item in leg.fills}.values()
                ),
                Decimal(0),
            )
            await self._fee_reconciliation.compare(estimated, actual)

        if record.state not in {
            ExecutionState.PARTIALLY_HEDGED,
            ExecutionState.EXCEPTION,
        }:
            return None

        timestamp = occurred_at or now
        if timestamp is None:
            raise ValueError("incident timestamp is required")
        key = f"execution:{record.correlation_id}:{record.state.value}"
        simulated = mode is not TradingMode.LIMITED_AUTO
        action = "investigate"
        if record.state is ExecutionState.PARTIALLY_HEDGED:
            action = "simulate_hedge" if simulated else "hedge"
        incident, claimed = await self._incidents.claim(
            ExecutionIncident(
                idempotency_key=key,
                correlation_id=record.correlation_id,
                state=record.state,
                action=action,
                simulated=simulated,
                unhedged_quantity=str(record.unhedged_quantity),
                occurred_at=timestamp,
            )
        )
        if not claimed:
            await self._enqueue(incident)
            return incident

        try:
            await self._system_control.disable_opening_async("execution incident")
            if (
                record.state is ExecutionState.PARTIALLY_HEDGED
                and self._emergency_service_factory is not None
                and evidence is not None
            ):
                await self._emergency_service_factory(mode).resolve(
                    _exposure(record, evidence),
                    maximum_unhedged_loss,
                )
        finally:
            # Escalation must survive a venue or control-store failure. A retry
            # observes the claimed incident and heals a missing outbox row.
            await self._enqueue(incident)
        return incident

    async def _enqueue(self, incident: ExecutionIncident) -> None:
        await self._notifications.enqueue_async(
            incident.idempotency_key,
            f"execution.{incident.state.value}",
            {
                "correlation_id": incident.correlation_id,
                "state": incident.state.value,
                "action": incident.action,
                "simulated": incident.simulated,
                "unhedged_quantity": incident.unhedged_quantity,
            },
        )


def _exposure(
    record: ExecutionRecord,
    evidence: ExecutionEvidence,
) -> UnhedgedExposure:
    kalshi_leg = record.legs.get(Venue.KALSHI)
    polymarket_leg = record.legs.get(Venue.POLYMARKET)
    kalshi_filled = Decimal(0) if kalshi_leg is None else kalshi_leg.filled_quantity
    polymarket_filled = (
        Decimal(0) if polymarket_leg is None else polymarket_leg.filled_quantity
    )
    if kalshi_filled < polymarket_filled:
        return UnhedgedExposure(
            correlation_id=record.correlation_id,
            quantity=record.unhedged_quantity,
            missing_venue=Venue.KALSHI,
            missing_market_id=evidence.kalshi_market_id,
            missing_outcome=evidence.kalshi_outcome,
            hedge_limit_price=evidence.kalshi_limit_price,
            hedge_worst_case_loss=record.unhedged_quantity
            * evidence.kalshi_limit_price,
            filled_venue=Venue.POLYMARKET,
            filled_market_id=evidence.polymarket_market_id,
            filled_outcome=evidence.polymarket_outcome,
            close_limit_price=evidence.polymarket_limit_price,
        )
    return UnhedgedExposure(
        correlation_id=record.correlation_id,
        quantity=record.unhedged_quantity,
        missing_venue=Venue.POLYMARKET,
        missing_market_id=evidence.polymarket_market_id,
        missing_outcome=evidence.polymarket_outcome,
        hedge_limit_price=evidence.polymarket_limit_price,
        hedge_worst_case_loss=record.unhedged_quantity
        * evidence.polymarket_limit_price,
        filled_venue=Venue.KALSHI,
        filled_market_id=evidence.kalshi_market_id,
        filled_outcome=evidence.kalshi_outcome,
        close_limit_price=evidence.kalshi_limit_price,
    )
