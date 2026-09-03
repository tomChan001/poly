from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.domain.enums import ExecutionState, MappingStatus, Venue
from backend.app.services.execution import (
    AuthorizationRejected,
    ControlledExecutionService,
    ExecutionAuthorizationService,
    ExecutionEvidence,
    ExecutionRecord,
    FillReport,
    InMemoryExecutionStore,
    OrderStatus,
    OrderSubmissionResult,
)
from backend.app.services.system_control import SystemControl

NOW = datetime(2026, 8, 18, 2, 0, tzinfo=UTC)


def evidence() -> ExecutionEvidence:
    return ExecutionEvidence(
        quote_evaluation_id="quote-1",
        rule_versions=("kalshi-rule-1", "poly-rule-1"),
        book_sequences=("kalshi-book-10", "poly-book-20"),
        balance_versions=("kalshi-balance-3", "poly-balance-4"),
        risk_policy_version="risk-1",
        capital_reservation_id="reserve-1",
        quantity=Decimal(10),
        kalshi_market_id="K-MARKET",
        polymarket_market_id="P-MARKET",
        kalshi_outcome="NO",
        polymarket_outcome="YES",
        kalshi_limit_price=Decimal("0.70"),
        polymarket_limit_price=Decimal("0.20"),
        conservative_roi=Decimal("0.08"),
        minimum_roi=Decimal("0.03"),
    )


class FakeTradingPort:
    def __init__(self, result: OrderSubmissionResult) -> None:
        self.result = result
        self.submissions = 0

    async def submit_fok(self, request: object) -> OrderSubmissionResult:
        self.submissions += 1
        return self.result

    async def find_by_client_order_id(self, client_order_id: str) -> OrderSubmissionResult | None:
        return self.result


def filled(venue: Venue, quantity: Decimal = Decimal(10)) -> OrderSubmissionResult:
    return OrderSubmissionResult(
        client_order_id=f"client-{venue}",
        status=OrderStatus.FILLED,
        fills=(FillReport(f"fill-{venue}", quantity, Decimal("0.50"), Decimal("0.01")),),
    )


def rejected(venue: Venue) -> OrderSubmissionResult:
    return OrderSubmissionResult(f"client-{venue}", OrderStatus.REJECTED, ())


def test_authorization_is_exact_only_short_lived_and_single_use() -> None:
    authorizations = ExecutionAuthorizationService()
    authorization = authorizations.issue(MappingStatus.EXACT, evidence(), NOW)

    assert authorization.expires_at == NOW + timedelta(seconds=2)
    authorizations.consume(authorization, evidence(), NOW + timedelta(seconds=1))

    with pytest.raises(AuthorizationRejected, match="already used"):
        authorizations.consume(authorization, evidence(), NOW + timedelta(seconds=1))

    with pytest.raises(AuthorizationRejected, match="EXACT"):
        authorizations.issue(MappingStatus.CONDITIONAL, evidence(), NOW)


@pytest.mark.asyncio
async def test_recovery_candidates_skip_settled_terminal_records() -> None:
    store = InMemoryExecutionStore()
    settled = ExecutionRecord(
        correlation_id="settled",
        state=ExecutionState.PAIRED,
        requested_quantity=Decimal(10),
        capital_settled=True,
    )
    unsettled = ExecutionRecord(
        correlation_id="unsettled",
        state=ExecutionState.EXCEPTION,
        requested_quantity=Decimal(10),
    )
    submitted = ExecutionRecord(
        correlation_id="submitted",
        state=ExecutionState.SUBMITTED,
        requested_quantity=Decimal(10),
        capital_settled=True,
    )
    for record in (settled, unsettled, submitted):
        await store.save(record)

    candidates = await store.list_recovery_candidates()

    assert {record.correlation_id for record in candidates} == {
        "unsettled",
        "submitted",
    }


def test_authorization_rejects_changed_execution_evidence() -> None:
    authorizations = ExecutionAuthorizationService()
    authorization = authorizations.issue(MappingStatus.EXACT, evidence(), NOW)
    changed = evidence()
    changed = ExecutionEvidence(
        **{**changed.as_dict(), "book_sequences": ("new-book", "poly-book-20")},
    )

    with pytest.raises(AuthorizationRejected, match="evidence changed"):
        authorizations.consume(authorization, changed, NOW + timedelta(seconds=1))


def test_authorization_is_expired_at_its_exact_deadline() -> None:
    authorizations = ExecutionAuthorizationService()
    authorization = authorizations.issue(MappingStatus.EXACT, evidence(), NOW)

    with pytest.raises(AuthorizationRejected, match="expired"):
        authorizations.consume(authorization, evidence(), NOW + timedelta(seconds=2))


@pytest.mark.asyncio
async def test_two_filled_legs_are_paired_with_stable_client_order_ids() -> None:
    control = SystemControl(opening_enabled=True)
    ports = {
        Venue.KALSHI: FakeTradingPort(filled(Venue.KALSHI)),
        Venue.POLYMARKET: FakeTradingPort(filled(Venue.POLYMARKET)),
    }
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(ports, control)

    result = await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert result.state is ExecutionState.PAIRED
    assert result.matched_quantity == Decimal(10)
    assert result.legs[Venue.KALSHI].client_order_id.endswith("-kalshi")
    assert result.legs[Venue.POLYMARKET].client_order_id.endswith("-polymarket")
    assert [transition.target for transition in result.transitions] == [
        ExecutionState.SUBMITTED,
        ExecutionState.PAIRED,
    ]


@pytest.mark.asyncio
async def test_submitted_state_is_persisted_before_any_venue_write() -> None:
    saved_states: list[ExecutionState] = []

    class RecordingStore:
        async def save(self, record: ExecutionRecord) -> None:
            saved_states.append(record.state)

        async def list(self) -> list[ExecutionRecord]:
            return []

        async def get(self, correlation_id: str) -> ExecutionRecord:
            raise KeyError(correlation_id)

    class OrderedPort(FakeTradingPort):
        async def submit_fok(self, request: object) -> OrderSubmissionResult:
            assert saved_states == [ExecutionState.SUBMITTED]
            return await super().submit_fok(request)

    ports = {
        Venue.KALSHI: OrderedPort(filled(Venue.KALSHI)),
        Venue.POLYMARKET: OrderedPort(filled(Venue.POLYMARKET)),
    }
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(
        ports,
        SystemControl(opening_enabled=True),
        RecordingStore(),
    )

    await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert saved_states == [ExecutionState.SUBMITTED, ExecutionState.PAIRED]


@pytest.mark.asyncio
async def test_disabling_opening_after_submitted_persistence_blocks_venue_writes() -> None:
    saved_states: list[ExecutionState] = []
    control = SystemControl(opening_enabled=True)

    class DisablingStore:
        async def save(self, record: ExecutionRecord) -> None:
            saved_states.append(record.state)
            if record.state is ExecutionState.SUBMITTED:
                control.disable_opening("test switch")

        async def list(self) -> list[ExecutionRecord]:
            return []

        async def get(self, correlation_id: str) -> ExecutionRecord:
            raise KeyError(correlation_id)

    ports = {
        Venue.KALSHI: FakeTradingPort(filled(Venue.KALSHI)),
        Venue.POLYMARKET: FakeTradingPort(filled(Venue.POLYMARKET)),
    }
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(
        ports,
        control,
        DisablingStore(),
    )

    with pytest.raises(AuthorizationRejected, match="real ordering is disabled"):
        await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert saved_states == [ExecutionState.SUBMITTED, ExecutionState.EXCEPTION]
    assert all(port.submissions == 0 for port in ports.values())


@pytest.mark.asyncio
async def test_second_guard_never_submits_when_exception_persistence_fails() -> None:
    saved_states: list[ExecutionState] = []
    control = SystemControl(opening_enabled=True)

    class FailingExceptionStore:
        async def save(self, record: ExecutionRecord) -> None:
            saved_states.append(record.state)
            if record.state is ExecutionState.SUBMITTED:
                control.disable_opening("test switch")
                return
            raise OSError("execution persistence unavailable")

        async def list(self) -> list[ExecutionRecord]:
            return []

        async def get(self, correlation_id: str) -> ExecutionRecord:
            raise KeyError(correlation_id)

    ports = {
        Venue.KALSHI: FakeTradingPort(filled(Venue.KALSHI)),
        Venue.POLYMARKET: FakeTradingPort(filled(Venue.POLYMARKET)),
    }
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(ports, control, FailingExceptionStore())

    with pytest.raises(OSError, match="execution persistence unavailable"):
        await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert saved_states == [ExecutionState.SUBMITTED, ExecutionState.EXCEPTION]
    assert all(port.submissions == 0 for port in ports.values())


@pytest.mark.asyncio
async def test_one_sided_fill_disables_opening_and_is_partially_hedged() -> None:
    control = SystemControl(opening_enabled=True)
    ports = {
        Venue.KALSHI: FakeTradingPort(filled(Venue.KALSHI)),
        Venue.POLYMARKET: FakeTradingPort(rejected(Venue.POLYMARKET)),
    }
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(ports, control)

    result = await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert result.state is ExecutionState.PARTIALLY_HEDGED
    assert result.matched_quantity == Decimal(0)
    assert result.unhedged_quantity == Decimal(10)
    assert control.opening_enabled is False
    assert control.reason == "partially hedged execution"


@pytest.mark.asyncio
async def test_real_submission_requires_opening_to_be_enabled() -> None:
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    ports = {
        Venue.KALSHI: FakeTradingPort(filled(Venue.KALSHI)),
        Venue.POLYMARKET: FakeTradingPort(filled(Venue.POLYMARKET)),
    }
    service = ControlledExecutionService(ports, SystemControl(opening_enabled=False))

    with pytest.raises(AuthorizationRejected, match="real ordering is disabled"):
        await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert all(port.submissions == 0 for port in ports.values())


@pytest.mark.asyncio
async def test_submitted_recovery_continues_while_opening_is_disabled() -> None:
    ports = {
        Venue.KALSHI: FakeTradingPort(filled(Venue.KALSHI)),
        Venue.POLYMARKET: FakeTradingPort(filled(Venue.POLYMARKET)),
    }
    record = ExecutionRecord(
        correlation_id="in-flight",
        state=ExecutionState.SUBMITTED,
        requested_quantity=Decimal(10),
        evidence=evidence(),
    )
    service = ControlledExecutionService(
        ports,
        SystemControl(opening_enabled=False),
    )

    recovered = await service.recover_submitted(record, NOW + timedelta(seconds=1))

    assert recovered.state is ExecutionState.PAIRED
    assert all(port.submissions == 0 for port in ports.values())


@pytest.mark.asyncio
async def test_unknown_leg_cannot_be_marked_paired_even_when_its_fills_match() -> None:
    unknown_with_fill = OrderSubmissionResult(
        "client-kalshi",
        OrderStatus.UNKNOWN,
        (FillReport("unknown-fill", Decimal(10), Decimal("0.50"), Decimal("0.01")),),
    )
    control = SystemControl(opening_enabled=True)
    ports = {
        Venue.KALSHI: FakeTradingPort(unknown_with_fill),
        Venue.POLYMARKET: FakeTradingPort(filled(Venue.POLYMARKET)),
    }
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(ports, control)

    result = await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert result.state is ExecutionState.EXCEPTION
    assert control.opening_enabled is False
    assert control.reason == "execution outcome unresolved"
