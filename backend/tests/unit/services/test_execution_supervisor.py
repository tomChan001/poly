from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.app.domain.enums import ExecutionState, Venue
from backend.app.services.execution import (
    ExecutionEvidence,
    ExecutionRecord,
    FillReport,
    OrderStatus,
    OrderSubmissionResult,
)
from backend.app.services.execution_supervisor import (
    ExecutionIncident,
    ExecutionSupervisor,
    InMemoryIncidentStore,
)
from backend.app.services.notifications import InMemoryOutbox, NotificationService
from backend.app.services.system_control import SystemControl


def hedge_evidence() -> ExecutionEvidence:
    return ExecutionEvidence(
        quote_evaluation_id="quote",
        rule_versions=("k", "p"),
        book_sequences=("1", "1"),
        balance_versions=("1", "1"),
        risk_policy_version="1",
        capital_reservation_id="reservation",
        quantity=Decimal(10),
        kalshi_market_id="K",
        polymarket_market_id="P",
        kalshi_outcome="no",
        polymarket_outcome="yes",
        kalshi_limit_price=Decimal("0.5"),
        polymarket_limit_price=Decimal("0.4"),
        conservative_roi=Decimal("0.1"),
        minimum_roi=Decimal("0.01"),
    )


@pytest.mark.asyncio
async def test_partial_fill_records_real_hedge_incident_and_outbox() -> None:
    control = SystemControl(opening_enabled=True)
    incidents = InMemoryIncidentStore()
    outbox = InMemoryOutbox()
    supervisor = ExecutionSupervisor(
        system_control=control,
        incidents=incidents,
        notifications=NotificationService(outbox),
    )
    record = ExecutionRecord(
        correlation_id="corr-partial",
        state=ExecutionState.PARTIALLY_HEDGED,
        requested_quantity=Decimal(10),
        matched_quantity=Decimal(6),
        unhedged_quantity=Decimal(4),
    )

    first = await supervisor.finalize(
        record,
        now=datetime(2026, 8, 26, tzinfo=UTC),
    )
    second = await supervisor.finalize(
        record,
        now=datetime(2026, 8, 26, tzinfo=UTC),
    )

    assert first is second
    assert first is not None
    assert first.action == "hedge"
    assert first.simulated is False
    assert control.opening_enabled is False
    assert len(incidents.records) == 1
    assert len(outbox.events) == 1
    event = outbox.events["execution:corr-partial:partially_hedged:not_required"]
    assert event.event_type == "execution.partially_hedged"
    assert "private_key" not in event.payload
    assert event.payload["remediation_status"] == "not_required"
    assert event.payload["remediation_venue"] is None
    assert event.payload["remediation_client_order_id"] is None


@pytest.mark.asyncio
async def test_incident_uses_the_active_submission_permission_without_relocking() -> None:
    control = SystemControl(opening_enabled=True)
    supervisor = ExecutionSupervisor(
        system_control=control,
        incidents=InMemoryIncidentStore(),
        notifications=NotificationService(InMemoryOutbox()),
    )
    record = ExecutionRecord(
        correlation_id="corr-scoped-close",
        state=ExecutionState.EXCEPTION,
        requested_quantity=Decimal(1),
    )

    async with control.opening_submission_guard() as permission:
        incident = await supervisor.finalize(
            record,
            now=datetime(2026, 8, 26, tzinfo=UTC),
            submission_permission=permission,
        )

    assert incident is not None
    assert control.opening_enabled is False


@pytest.mark.asyncio
async def test_paired_execution_reconciles_actual_fees_and_closes_opening() -> None:
    control = SystemControl(opening_enabled=True)
    supervisor = ExecutionSupervisor(
        system_control=control,
        incidents=InMemoryIncidentStore(),
        notifications=NotificationService(InMemoryOutbox()),
    )
    record = ExecutionRecord(
        correlation_id="corr-fee-variance",
        state=ExecutionState.PAIRED,
        requested_quantity=Decimal(1),
        matched_quantity=Decimal(1),
        legs={
            Venue.KALSHI: OrderSubmissionResult(
                "k",
                OrderStatus.FILLED,
                (FillReport("k-fill", Decimal(1), Decimal("0.5"), Decimal("0.04")),),
            ),
            Venue.POLYMARKET: OrderSubmissionResult(
                "p",
                OrderStatus.FILLED,
                (FillReport("p-fill", Decimal(1), Decimal("0.4"), Decimal("0.03")),),
            ),
        },
    )
    evidence = ExecutionEvidence(
        quote_evaluation_id="quote",
        rule_versions=("k", "p"),
        book_sequences=("1", "1"),
        balance_versions=("1", "1"),
        risk_policy_version="1",
        capital_reservation_id="reservation",
        quantity=Decimal(1),
        kalshi_market_id="K",
        polymarket_market_id="P",
        kalshi_outcome="no",
        polymarket_outcome="yes",
        kalshi_limit_price=Decimal("0.5"),
        polymarket_limit_price=Decimal("0.4"),
        conservative_roi=Decimal("0.1"),
        minimum_roi=Decimal("0.01"),
        estimated_fees=(Decimal("0.01"), Decimal("0.01")),
    )

    incident = await supervisor.finalize(
        record,
        evidence,
        now=datetime(2026, 8, 26, tzinfo=UTC),
    )

    assert incident is None
    assert control.opening_enabled is False
    assert control.reason == "actual fee differs from estimate"


@pytest.mark.asyncio
async def test_claimed_incident_retries_fail_closed_remediation_after_restart() -> None:
    class FailingOnceControl(SystemControl):
        def __init__(self) -> None:
            super().__init__(opening_enabled=True)
            self.attempts = 0

        async def disable_opening_async(self, reason: str, **kwargs: object) -> object:
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("control store unavailable")
            return await super().disable_opening_async(reason, **kwargs)

    control = FailingOnceControl()
    incidents = InMemoryIncidentStore()
    notifications = NotificationService(InMemoryOutbox())
    record = ExecutionRecord(
        correlation_id="crashed-remediation",
        state=ExecutionState.EXCEPTION,
        requested_quantity=Decimal(10),
    )
    first_supervisor = ExecutionSupervisor(control, incidents, notifications)

    with pytest.raises(OSError, match="control store unavailable"):
        await first_supervisor.finalize(record, now=datetime(2026, 8, 26, tzinfo=UTC))

    restarted_supervisor = ExecutionSupervisor(control, incidents, notifications)
    incident = await restarted_supervisor.finalize(
        record,
        now=datetime(2026, 8, 26, tzinfo=UTC),
    )

    assert incident is not None
    assert control.attempts == 2
    assert control.opening_enabled is False


@pytest.mark.asyncio
async def test_claim_failure_occurs_after_opening_is_failed_closed() -> None:
    class FailingClaimStore(InMemoryIncidentStore):
        async def claim(self, incident):
            raise RuntimeError("incident store unavailable")

    control = SystemControl(opening_enabled=True)
    supervisor = ExecutionSupervisor(
        control,
        FailingClaimStore(),
        NotificationService(InMemoryOutbox()),
    )
    record = ExecutionRecord(
        correlation_id="claim-failure",
        state=ExecutionState.EXCEPTION,
        requested_quantity=Decimal(10),
    )

    with pytest.raises(RuntimeError, match="incident store unavailable"):
        await supervisor.finalize(record, now=datetime(2026, 8, 26, tzinfo=UTC))

    assert control.opening_enabled is False


@pytest.mark.asyncio
async def test_control_persistence_failure_still_claims_and_attempts_remediation() -> None:
    class FailingControl(SystemControl):
        async def disable_opening_async(self, reason: str, **kwargs: object) -> object:
            self.disable_opening(reason)
            raise OSError("control persistence unavailable")

    class RecordingEmergency:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve(self, *args: object, **kwargs: object) -> None:
            self.calls += 1

    control = FailingControl(opening_enabled=True)
    incidents = InMemoryIncidentStore()
    emergency = RecordingEmergency()
    supervisor = ExecutionSupervisor(
        control,
        incidents,
        NotificationService(InMemoryOutbox()),
        emergency_service_factory=lambda: emergency,
    )
    record = ExecutionRecord(
        correlation_id="control-persistence-failure",
        state=ExecutionState.PARTIALLY_HEDGED,
        requested_quantity=Decimal(10),
        matched_quantity=Decimal(6),
        unhedged_quantity=Decimal(4),
        legs={
            Venue.KALSHI: OrderSubmissionResult("k", OrderStatus.FILLED, ()),
            Venue.POLYMARKET: OrderSubmissionResult("p", OrderStatus.PARTIAL, ()),
        },
    )

    with pytest.raises(OSError, match="control persistence unavailable"):
        await supervisor.finalize(
            record,
            hedge_evidence(),
            now=datetime(2026, 8, 26, tzinfo=UTC),
        )

    assert control.opening_enabled is False
    assert len(incidents.records) == 1
    assert emergency.calls == 1


@pytest.mark.asyncio
async def test_remediation_status_changes_produce_distinct_observable_notifications() -> None:
    class EventuallyVisiblePort:
        def __init__(self) -> None:
            self.submissions = 0
            self.lookups = 0

        async def submit_fok(self, request):
            self.submissions += 1
            raise AssertionError("started remediation must only reconcile")

        async def find_by_client_order_id(self, client_order_id: str):
            self.lookups += 1
            if self.lookups == 1:
                return None
            return OrderSubmissionResult(
                client_order_id,
                OrderStatus.FILLED,
                (FillReport("reconciled", Decimal(4), Decimal("0.4"), Decimal(0)),),
            )

    control = SystemControl(opening_enabled=True)
    incidents = InMemoryIncidentStore()
    outbox = InMemoryOutbox()
    record = ExecutionRecord(
        correlation_id="observable-remediation",
        state=ExecutionState.PARTIALLY_HEDGED,
        requested_quantity=Decimal(10),
        matched_quantity=Decimal(6),
        unhedged_quantity=Decimal(4),
    )
    key = "execution:observable-remediation:partially_hedged"
    await incidents.claim(
        ExecutionIncident(
            idempotency_key=key,
            correlation_id=record.correlation_id,
            state=record.state,
            action="hedge",
            simulated=False,
            unhedged_quantity="4",
            occurred_at=datetime(2026, 8, 26, tzinfo=UTC),
        )
    )
    await incidents.start_remediation(
        key,
        "observable-remediation-emergency-hedge",
        Venue.POLYMARKET,
    )
    ports = {Venue.KALSHI: EventuallyVisiblePort(), Venue.POLYMARKET: EventuallyVisiblePort()}
    supervisor = ExecutionSupervisor(control, incidents, NotificationService(outbox))
    supervisor.bind_emergency_ports(ports)

    await supervisor.finalize(record, hedge_evidence(), now=datetime(2026, 8, 26, tzinfo=UTC))
    await supervisor.finalize(record, hedge_evidence(), now=datetime(2026, 8, 26, tzinfo=UTC))

    assert set(outbox.events) == {f"{key}:unknown", f"{key}:resolved"}
    assert outbox.events[f"{key}:unknown"].payload["remediation_status"] == "unknown"
    resolved = outbox.events[f"{key}:resolved"].payload
    assert resolved["remediation_status"] == "resolved"
    assert resolved["remediation_venue"] == "polymarket"
    assert resolved["remediation_client_order_id"] == "observable-remediation-emergency-hedge"
    assert sum(port.submissions for port in ports.values()) == 0
