import asyncio
import builtins
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from backend.app.domain.enums import ExecutionState, MappingStatus, Venue
from backend.app.domain.models import validate_transition
from backend.app.services.system_control import (
    OpeningSubmissionPermission,
    SystemControl,
)


class AuthorizationRejected(ValueError):
    pass


class OrderOutcomeUnknown(RuntimeError):
    """The venue may have accepted the order even though no response arrived."""


class ExecutionSubmissionClaimed(RuntimeError):
    """Another worker owns submission; do not recover or release its reservation."""


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
    estimated_fees: tuple[Decimal, Decimal] = (Decimal(0), Decimal(0))

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
        *,
        correlation_id: str | None = None,
    ) -> ExecutionAuthorization:
        if mapping_status is not MappingStatus.EXACT:
            raise AuthorizationRejected(
                "execution authorization requires an EXACT mapping"
            )
        if evidence.conservative_roi < evidence.minimum_roi:
            raise AuthorizationRejected("conservative ROI is below the policy minimum")

        return ExecutionAuthorization(
            id=str(uuid4()),
            correlation_id=correlation_id or str(uuid4()),
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
    evidence: ExecutionEvidence | None = None
    legs: dict[Venue, OrderSubmissionResult] = field(default_factory=dict)
    matched_quantity: Decimal = Decimal(0)
    unhedged_quantity: Decimal = Decimal(0)
    transitions: list[StateTransition] = field(default_factory=list)
    capital_settled: bool = False

    def transition(self, target: ExecutionState, occurred_at: datetime) -> None:
        validate_transition(self.state, target)
        self.transitions.append(StateTransition(self.state, target, occurred_at))
        self.state = target


class ExecutionStore(Protocol):
    def execution_guard(
        self, correlation_id: str
    ) -> AbstractAsyncContextManager[object]: ...

    def owns_execution_lease(self, lease: object, correlation_id: str) -> bool: ...

    async def claim_submission(self, record: ExecutionRecord) -> bool: ...

    async def save(self, record: ExecutionRecord) -> None: ...

    async def list(self) -> list[ExecutionRecord]: ...

    async def list_recovery_candidates(self) -> builtins.list[ExecutionRecord]: ...

    async def get(self, correlation_id: str) -> ExecutionRecord: ...


class ExecutionSupervisorPort(Protocol):
    def bind_emergency_ports(
        self,
        ports: Mapping[Venue, ExecutionTradingPort],
    ) -> None: ...

    async def finalize(
        self,
        record: ExecutionRecord,
        evidence: ExecutionEvidence | None = None,
        *,
        now: datetime,
        maximum_unhedged_loss: Decimal = Decimal(0),
        submission_permission: OpeningSubmissionPermission | None = None,
    ) -> object | None: ...


class InMemoryExecutionStore:
    def __init__(self) -> None:
        self._records: dict[str, ExecutionRecord] = {}
        self._claim_lock = asyncio.Lock()
        self._execution_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def execution_guard(self, correlation_id: str) -> AsyncIterator[object]:
        """Fence one recovery/submission identity until its terminal save.

        Lock order for new openings is submission fence -> this lock ->
        capital/venue I/O. Recovery only takes this lock.
        """
        lock = self._execution_locks.setdefault(correlation_id, asyncio.Lock())
        async with lock:
            scope = _ActiveExecutionScope()
            try:
                yield _ExecutionLease(self, correlation_id, scope)
            finally:
                scope.active = False

    def owns_execution_lease(self, lease: object, correlation_id: str) -> bool:
        return isinstance(lease, _ExecutionLease) and (
            lease.owner is self and lease.correlation_id == correlation_id
            and lease.scope.active
        )

    async def claim_submission(self, record: ExecutionRecord) -> bool:
        async with self._claim_lock:
            if record.correlation_id in self._records:
                return False
            self._records.setdefault(record.correlation_id, record)
            return True

    async def save(self, record: ExecutionRecord) -> None:
        self._records[record.correlation_id] = record

    async def list(self) -> list[ExecutionRecord]:
        return list(self._records.values())

    async def list_recovery_candidates(self) -> builtins.list[ExecutionRecord]:
        return [
            record
            for record in self._records.values()
            if record.state is ExecutionState.SUBMITTED
            or (
                record.state
                in {
                    ExecutionState.PAIRED,
                    ExecutionState.PARTIALLY_HEDGED,
                    ExecutionState.EXCEPTION,
                    ExecutionState.CANCELLED,
                }
                and not record.capital_settled
            )
        ]

    async def get(self, correlation_id: str) -> ExecutionRecord:
        return self._records[correlation_id]


@dataclass(frozen=True, slots=True)
class _ExecutionLease:
    owner: object
    correlation_id: str
    scope: "_ActiveExecutionScope"


class _ActiveExecutionScope:
    active = True


class ControlledExecutionService:
    _venue_timeout_seconds = 10.0
    def __init__(
        self,
        ports: Mapping[Venue, ExecutionTradingPort],
        system_control: SystemControl,
        store: ExecutionStore | None = None,
        supervisor: ExecutionSupervisorPort | None = None,
        maximum_unhedged_loss: Decimal = Decimal(0),
    ) -> None:
        self._ports = ports
        self._system_control = system_control
        self._authorizations = ExecutionAuthorizationService()
        self._store = store
        self._supervisor = supervisor
        self._maximum_unhedged_loss = maximum_unhedged_loss
        self._pending_disable_reason: str | None = None

    async def execute(
        self,
        authorization: ExecutionAuthorization,
        current_evidence: ExecutionEvidence,
        now: datetime,
        *,
        submission_permission: OpeningSubmissionPermission | None = None,
        execution_lease: object | None = None,
    ) -> ExecutionRecord:
        self._authorizations.consume(authorization, current_evidence, now)
        # The opening state is durable and may have changed in another
        # process since this runtime's previous cycle.  Refresh before the
        # submission claim so an OFF state cannot create a recovery record.
        if submission_permission is None:
            await self._system_control.refresh_async()
            if not self._system_control.opening_enabled:
                raise AuthorizationRejected("real ordering is disabled")
            async with self._system_control.opening_submission_guard() as permission:
                record = await self._execute_with_permission(
                    authorization,
                    current_evidence,
                    now,
                    permission,
                    execution_lease,
                )
            await self._apply_pending_disable()
            return record
        return await self._execute_with_permission(
            authorization,
            current_evidence,
            now,
            submission_permission,
            execution_lease,
        )

    def take_pending_disable_reason(self) -> str | None:
        reason = self._pending_disable_reason
        self._pending_disable_reason = None
        return reason

    async def _apply_pending_disable(self) -> None:
        reason = self.take_pending_disable_reason()
        if reason is not None:
            await self._system_control.disable_opening_async(reason)

    async def _execute_with_permission(
        self,
        authorization: ExecutionAuthorization,
        current_evidence: ExecutionEvidence,
        now: datetime,
        permission: OpeningSubmissionPermission,
        execution_lease: object | None,
    ) -> ExecutionRecord:
        if not self._system_control.owns_submission_permission(permission):
            raise AuthorizationRejected("submission permission is not active")
        if not permission.allowed:
            raise AuthorizationRejected("real ordering is disabled")
        guard = (
            None
            if self._store is None
            else getattr(self._store, "execution_guard", None)
        )
        if guard is not None and execution_lease is None:
            async with guard(authorization.correlation_id) as lease:
                return await self._execute_with_permission(
                    authorization,
                    current_evidence,
                    now,
                    permission,
                    lease,
                )
        owns_lease = (
            None
            if self._store is None
            else getattr(self._store, "owns_execution_lease", None)
        )
        if owns_lease is not None and not owns_lease(
            execution_lease, authorization.correlation_id
        ):
            raise AuthorizationRejected("execution lease is not active")
        requests = self._requests(authorization)
        record = ExecutionRecord(
            correlation_id=authorization.correlation_id,
            state=ExecutionState.PRECHECKED,
            requested_quantity=current_evidence.quantity,
            evidence=current_evidence,
        )
        record.transition(ExecutionState.SUBMITTED, now)
        # Atomically claim the recovery identity before either venue request.
        # A losing worker must leave the winner's record and capital reservation
        # untouched; a later recovery cycle reconciles it.
        if self._store is not None and not await self._store.claim_submission(record):
            raise ExecutionSubmissionClaimed(authorization.correlation_id)

        # Preserve the synchronous in-process emergency hook used by the
        # local runtime. Durable OFF updates are serialized by the outer
        # permission; this catches a local hook that fires after the claim.
        if not self._system_control.opening_enabled:
            record.transition(ExecutionState.EXCEPTION, now)
            if self._store is not None:
                await self._store.save(record)
            raise AuthorizationRejected("real ordering is disabled")

        results = await asyncio.gather(
            *(self._submit_or_recover(request) for request in requests.values()),
        )
        record.legs = dict(zip(requests, results, strict=True))
        await self._finalize(record, now, current_evidence, permission)
        return record

    async def recover_submitted(
        self,
        record: ExecutionRecord,
        now: datetime,
        *,
        submission_permission: OpeningSubmissionPermission | None = None,
    ) -> ExecutionRecord:
        if record.state is not ExecutionState.SUBMITTED:
            raise ValueError("only submitted executions can be recovered")

        # Recovery is reconciliation, not a new opening. It continues when
        # real-order opening is disabled and never resubmits.
        for venue in Venue:
            client_order_id = f"{record.correlation_id}-{venue.value}"
            result = await self._ports[venue].find_by_client_order_id(client_order_id)
            record.legs[venue] = (
                OrderSubmissionResult(client_order_id, OrderStatus.UNKNOWN, ())
                if result is None
                else replace(result, client_order_id=client_order_id)
            )
        await self._finalize(record, now, record.evidence, submission_permission)
        return record

    async def _finalize(
        self,
        record: ExecutionRecord,
        now: datetime,
        evidence: ExecutionEvidence | None = None,
        submission_permission: OpeningSubmissionPermission | None = None,
    ) -> None:
        quantities = [record.legs[venue].filled_quantity for venue in Venue]
        record.matched_quantity = min(quantities)
        record.unhedged_quantity = max(quantities) - record.matched_quantity

        has_unknown_outcome = any(
            leg.status is OrderStatus.UNKNOWN for leg in record.legs.values()
        )
        if has_unknown_outcome:
            record.transition(ExecutionState.EXCEPTION, now)
        elif all(quantity == record.requested_quantity for quantity in quantities):
            record.transition(ExecutionState.PAIRED, now)
        elif record.unhedged_quantity > 0:
            record.transition(ExecutionState.PARTIALLY_HEDGED, now)
            if self._supervisor is None:
                self._pending_disable_reason = "partially hedged execution"
        else:
            record.transition(ExecutionState.EXCEPTION, now)
        if has_unknown_outcome and not (
            self._supervisor is not None
            and record.state
            in {ExecutionState.PARTIALLY_HEDGED, ExecutionState.EXCEPTION}
        ):
            self._pending_disable_reason = "execution outcome unresolved"
        if self._store is not None:
            await self._store.save(record)
        if self._supervisor is not None:
            await self._supervisor.finalize(
                record,
                evidence,
                now=now,
                maximum_unhedged_loss=self._maximum_unhedged_loss,
                submission_permission=submission_permission,
            )

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
            result = await asyncio.wait_for(
                port.submit_fok(request), timeout=self._venue_timeout_seconds
            )
        except Exception:  # noqa: BLE001 - any post-write failure can hide an accepted order
            # A timeout is not a rejection. Querying by the stable client ID is
            # the only safe recovery path because retrying could double-fill.
            # asyncio cancellation derives from BaseException and is not caught.
            try:
                recovered = await asyncio.wait_for(
                    port.find_by_client_order_id(request.client_order_id),
                    timeout=self._venue_timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - unavailable reconciliation remains UNKNOWN
                recovered = None
            if recovered is None:
                return OrderSubmissionResult(
                    request.client_order_id, OrderStatus.UNKNOWN, ()
                )
            result = recovered
        return replace(result, client_order_id=request.client_order_id)
