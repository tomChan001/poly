from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from backend.app.domain.enums import Venue
from backend.app.services.execution import (
    ExecutionTradingPort,
    OrderAction,
    OrderRequest,
    OrderStatus,
    OrderSubmissionResult,
)


class EmergencyAction(StrEnum):
    HEDGE = "hedge"
    CLOSE = "close"


class RemediationStatus(StrEnum):
    STARTED = "started"
    RESOLVED = "resolved"
    UNKNOWN = "unknown"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EmergencyRemediation:
    idempotency_key: str
    client_order_id: str
    venue: Venue
    status: RemediationStatus


class EmergencyRemediationStore(Protocol):
    async def get_remediation(
        self,
        idempotency_key: str,
    ) -> EmergencyRemediation | None: ...

    async def start_remediation(
        self,
        idempotency_key: str,
        client_order_id: str,
        venue: Venue,
    ) -> tuple[EmergencyRemediation, bool]: ...

    async def complete_remediation(
        self,
        idempotency_key: str,
        status: RemediationStatus,
    ) -> EmergencyRemediation: ...


class InMemoryEmergencyRemediationStore:
    def __init__(self) -> None:
        self.records: dict[str, EmergencyRemediation] = {}

    async def get_remediation(
        self,
        idempotency_key: str,
    ) -> EmergencyRemediation | None:
        return self.records.get(idempotency_key)

    async def start_remediation(
        self,
        idempotency_key: str,
        client_order_id: str,
        venue: Venue,
    ) -> tuple[EmergencyRemediation, bool]:
        existing = self.records.get(idempotency_key)
        if existing is not None:
            return existing, False
        remediation = EmergencyRemediation(
            idempotency_key,
            client_order_id,
            venue,
            RemediationStatus.STARTED,
        )
        self.records[idempotency_key] = remediation
        return remediation, True

    async def complete_remediation(
        self,
        idempotency_key: str,
        status: RemediationStatus,
    ) -> EmergencyRemediation:
        current = self.records[idempotency_key]
        if current.status is RemediationStatus.RESOLVED:
            return current
        completed = EmergencyRemediation(
            current.idempotency_key,
            current.client_order_id,
            current.venue,
            status,
        )
        self.records[idempotency_key] = completed
        return completed


@dataclass(frozen=True, slots=True)
class UnhedgedExposure:
    correlation_id: str
    quantity: Decimal
    missing_venue: Venue
    missing_market_id: str
    missing_outcome: str
    hedge_limit_price: Decimal
    hedge_worst_case_loss: Decimal
    filled_venue: Venue
    filled_market_id: str
    filled_outcome: str
    close_limit_price: Decimal


@dataclass(frozen=True, slots=True)
class EmergencyResult:
    action: EmergencyAction
    order: OrderSubmissionResult
    resolved: bool
    simulated: bool = False


class EmergencyHedgeService:
    def __init__(
        self,
        ports: dict[Venue, ExecutionTradingPort],
        *,
        remediation_store: EmergencyRemediationStore,
    ) -> None:
        self._ports = ports
        self._remediations = remediation_store
        self._results: dict[str, EmergencyResult] = {}

    async def resolve(
        self,
        exposure: UnhedgedExposure,
        maximum_unhedged_loss: Decimal,
        *,
        remediation_key: str | None = None,
    ) -> EmergencyResult:
        existing = self._results.get(exposure.correlation_id)
        if existing is not None:
            return existing

        if exposure.hedge_worst_case_loss <= maximum_unhedged_loss:
            action = EmergencyAction.HEDGE
            request = OrderRequest(
                venue=exposure.missing_venue,
                client_order_id=f"{exposure.correlation_id}-emergency-hedge",
                market_id=exposure.missing_market_id,
                outcome=exposure.missing_outcome,
                quantity=exposure.quantity,
                limit_price=exposure.hedge_limit_price,
                action=OrderAction.BUY,
            )
        else:
            # When buying the missing leg breaches the loss budget, reduce the
            # exposure by selling the filled leg. Human escalation still owns
            # any result that is not a complete immediate fill.
            action = EmergencyAction.CLOSE
            request = OrderRequest(
                venue=exposure.filled_venue,
                client_order_id=f"{exposure.correlation_id}-emergency-close",
                market_id=exposure.filled_market_id,
                outcome=exposure.filled_outcome,
                quantity=exposure.quantity,
                limit_price=exposure.close_limit_price,
                action=OrderAction.SELL,
            )

        key = remediation_key or exposure.correlation_id
        remediation, started_now = await self._remediations.start_remediation(
            key,
            request.client_order_id,
            request.venue,
        )
        port = self._ports[request.venue]
        if started_now:
            try:
                order = await port.submit_fok(request)
            except Exception:  # noqa: BLE001 - every post-write failure is ambiguous
                order = await self._reconcile(port, request.client_order_id)
        elif remediation.status in {
            RemediationStatus.STARTED,
            RemediationStatus.UNKNOWN,
        }:
            # A prior process may have submitted after persisting the intent.
            # It is never safe to issue another emergency order from this state.
            order = await self._reconcile(port, remediation.client_order_id)
        else:
            result = EmergencyResult(
                action=action,
                order=_terminal_order(remediation),
                resolved=remediation.status is RemediationStatus.RESOLVED,
            )
            self._results[exposure.correlation_id] = result
            return result
        result = EmergencyResult(
            action=action,
            order=order,
            resolved=(
                order.status is OrderStatus.FILLED
                and order.filled_quantity == exposure.quantity
            ),
        )
        await self._remediations.complete_remediation(
            key,
            _disposition(result),
        )
        # Recording before returning makes retries after an API timeout observe
        # one stable disposition instead of sending a second emergency order.
        if _disposition(result) is not RemediationStatus.UNKNOWN:
            self._results[exposure.correlation_id] = result
        return result

    async def _reconcile(
        self,
        port: ExecutionTradingPort,
        client_order_id: str,
    ) -> OrderSubmissionResult:
        try:
            recovered = await port.find_by_client_order_id(client_order_id)
        except Exception:  # noqa: BLE001 - unavailable query remains UNKNOWN
            recovered = None
        return (
            OrderSubmissionResult(client_order_id, OrderStatus.UNKNOWN, ())
            if recovered is None
            else recovered
        )


def _disposition(result: EmergencyResult) -> RemediationStatus:
    if result.resolved:
        return RemediationStatus.RESOLVED
    if result.order.status is OrderStatus.UNKNOWN:
        return RemediationStatus.UNKNOWN
    return RemediationStatus.FAILED


def _terminal_order(remediation: EmergencyRemediation) -> OrderSubmissionResult:
    status = (
        OrderStatus.FILLED
        if remediation.status is RemediationStatus.RESOLVED
        else OrderStatus.UNKNOWN
    )
    return OrderSubmissionResult(remediation.client_order_id, status, ())
