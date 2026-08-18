import asyncio
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from backend.app.core.config import TradingMode
from backend.app.domain.enums import ExecutionState, MappingStatus, Venue
from backend.app.domain.models import validate_transition
from backend.app.services.system_control import SystemControl


class AuthorizationRejected(ValueError):
    pass


class OrderOutcomeUnknown(RuntimeError):
    """The venue may have accepted the order even though no response arrived."""


class OrderStatus(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class OrderAction(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    quote_evaluation_id: str
    rule_versions: tuple[str, str]
    book_sequences: tuple[str, str]
    balance_versions: tuple[str, str]
    risk_policy_version: str
    capital_reservation_id: str
    quantity: Decimal
    kalshi_market_id: str
    polymarket_market_id: str
    kalshi_outcome: str
    polymarket_outcome: str
    kalshi_limit_price: Decimal
    polymarket_limit_price: Decimal
    conservative_roi: Decimal
    minimum_roi: Decimal

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ExecutionAuthorization:
    id: str
    correlation_id: str
    evidence: ExecutionEvidence
    issued_at: datetime
    expires_at: datetime
    used_at: datetime | None = None


class ExecutionAuthorizationService:
    def issue(
        self,
        mapping_status: MappingStatus,
        evidence: ExecutionEvidence,
        now: datetime,
    ) -> ExecutionAuthorization:
        if mapping_status is not MappingStatus.EXACT:
            raise AuthorizationRejected("execution authorization requires an EXACT mapping")
        if evidence.conservative_roi < evidence.minimum_roi:
            raise AuthorizationRejected("conservative ROI is below the policy minimum")

        return ExecutionAuthorization(
            id=str(uuid4()),
            correlation_id=str(uuid4()),
            evidence=evidence,
            issued_at=now,
            expires_at=now + timedelta(seconds=2),
        )

    def consume(
        self,
        authorization: ExecutionAuthorization,
        current_evidence: ExecutionEvidence,
        now: datetime,
    ) -> None:
        if authorization.used_at is not None:
            raise AuthorizationRejected("execution authorization was already used")
        if now >= authorization.expires_at:
            raise AuthorizationRejected("execution authorization expired")
        if authorization.evidence != current_evidence:
            raise AuthorizationRejected("execution evidence changed")

        authorization.used_at = now


@dataclass(frozen=True, slots=True)
class OrderRequest:
    venue: Venue
    client_order_id: str
    market_id: str
    outcome: str
    quantity: Decimal
    limit_price: Decimal
    action: OrderAction = OrderAction.BUY


@dataclass(frozen=True, slots=True)
class FillReport:
    fill_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal


@dataclass(frozen=True, slots=True)
class OrderSubmissionResult:
    client_order_id: str
    status: OrderStatus
    fills: tuple[FillReport, ...]

    @property
    def filled_quantity(self) -> Decimal:
        # User streams and REST reconciliation can report the same fill. Venue
        # fill IDs are the durable identity, so duplicates must not add risk.
        unique = {fill.fill_id: fill for fill in self.fills}
        return sum((fill.quantity for fill in unique.values()), Decimal(0))


class ExecutionTradingPort(Protocol):
    async def submit_fok(self, request: OrderRequest) -> OrderSubmissionResult: ...

    async def find_by_client_order_id(
        self,
        client_order_id: str,
    ) -> OrderSubmissionResult | None: ...


@dataclass(frozen=True, slots=True)
class StateTransition:
    source: ExecutionState
    target: ExecutionState
    occurred_at: datetime


@dataclass(slots=True)
class ExecutionRecord:
    correlation_id: str
    state: ExecutionState
    requested_quantity: Decimal
    legs: dict[Venue, OrderSubmissionResult] = field(default_factory=dict)
    matched_quantity: Decimal = Decimal(0)
    unhedged_quantity: Decimal = Decimal(0)
    transitions: list[StateTransition] = field(default_factory=list)

    def transition(self, target: ExecutionState, occurred_at: datetime) -> None:
        validate_transition(self.state, target)
        self.transitions.append(StateTransition(self.state, target, occurred_at))
        self.state = target


class InMemoryExecutionStore:
    def __init__(self) -> None:
        self._records: dict[str, ExecutionRecord] = {}

    def save(self, record: ExecutionRecord) -> None:
        self._records[record.correlation_id] = record

    def list(self) -> list[ExecutionRecord]:
        return list(self._records.values())

    def get(self, correlation_id: str) -> ExecutionRecord:
        return self._records[correlation_id]


class ControlledExecutionService:
    def __init__(
        self,
        ports: dict[Venue, ExecutionTradingPort],
        system_control: SystemControl,
        trading_mode: TradingMode,
        store: InMemoryExecutionStore | None = None,
    ) -> None:
        self._ports = ports
        self._system_control = system_control
        self._trading_mode = trading_mode
        self._authorizations = ExecutionAuthorizationService()
        self._store = store

    async def execute(
        self,
        authorization: ExecutionAuthorization,
        current_evidence: ExecutionEvidence,
        now: datetime,
    ) -> ExecutionRecord:
        if self._trading_mode is not TradingMode.LIMITED_AUTO:
            raise AuthorizationRejected("real submission requires limited_auto mode")
        if not self._system_control.opening_enabled:
            raise AuthorizationRejected("opening kill switch is disabled")

        self._authorizations.consume(authorization, current_evidence, now)
        requests = self._requests(authorization)
        record = ExecutionRecord(
            correlation_id=authorization.correlation_id,
            state=ExecutionState.PRECHECKED,
            requested_quantity=current_evidence.quantity,
        )
        record.transition(ExecutionState.SUBMITTED, now)

        results = await asyncio.gather(
            *(self._submit_or_recover(request) for request in requests.values()),
        )
        record.legs = dict(zip(requests, results, strict=True))
        self._finalize(record, now)
        return record

    async def recover_submitted(
        self,
        record: ExecutionRecord,
        now: datetime,
    ) -> ExecutionRecord:
        if record.state is not ExecutionState.SUBMITTED:
            raise ValueError("only submitted executions can be recovered")

        # Recovery is reconciliation, not a new opening. It must continue when
        # trading mode or the kill switch is off, and it must never resubmit.
        for venue in Venue:
            client_order_id = f"{record.correlation_id}-{venue.value}"
            result = await self._ports[venue].find_by_client_order_id(client_order_id)
            record.legs[venue] = (
                OrderSubmissionResult(client_order_id, OrderStatus.UNKNOWN, ())
                if result is None
                else replace(result, client_order_id=client_order_id)
            )
        self._finalize(record, now)
        return record

    def _finalize(self, record: ExecutionRecord, now: datetime) -> None:
        quantities = [record.legs[venue].filled_quantity for venue in Venue]
        record.matched_quantity = min(quantities)
        record.unhedged_quantity = max(quantities) - record.matched_quantity

        if all(quantity == record.requested_quantity for quantity in quantities):
            record.transition(ExecutionState.PAIRED, now)
        elif record.unhedged_quantity > 0:
            record.transition(ExecutionState.PARTIALLY_HEDGED, now)
            self._system_control.disable_opening("partially hedged execution")
        else:
            record.transition(ExecutionState.EXCEPTION, now)
        if any(leg.status is OrderStatus.UNKNOWN for leg in record.legs.values()):
            self._system_control.disable_opening("execution outcome unresolved")
        if self._store is not None:
            self._store.save(record)

    def _requests(
        self,
        authorization: ExecutionAuthorization,
    ) -> dict[Venue, OrderRequest]:
        evidence = authorization.evidence
        common = authorization.correlation_id
        return {
            Venue.KALSHI: OrderRequest(
                Venue.KALSHI,
                f"{common}-kalshi",
                evidence.kalshi_market_id,
                evidence.kalshi_outcome,
                evidence.quantity,
                evidence.kalshi_limit_price,
            ),
            Venue.POLYMARKET: OrderRequest(
                Venue.POLYMARKET,
                f"{common}-polymarket",
                evidence.polymarket_market_id,
                evidence.polymarket_outcome,
                evidence.quantity,
                evidence.polymarket_limit_price,
            ),
        }

    async def _submit_or_recover(self, request: OrderRequest) -> OrderSubmissionResult:
        port = self._ports[request.venue]
        try:
            result = await port.submit_fok(request)
        except Exception:  # noqa: BLE001 - any post-write failure can hide an accepted order
            # A timeout is not a rejection. Querying by the stable client ID is
            # the only safe recovery path because retrying could double-fill.
            # asyncio cancellation derives from BaseException and is not caught.
            try:
                recovered = await port.find_by_client_order_id(request.client_order_id)
            except Exception:  # noqa: BLE001 - unavailable reconciliation remains UNKNOWN
                recovered = None
            if recovered is None:
                return OrderSubmissionResult(request.client_order_id, OrderStatus.UNKNOWN, ())
            result = recovered
        return replace(result, client_order_id=request.client_order_id)
