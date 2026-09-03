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
    ExecutionSupervisor,
    InMemoryIncidentStore,
)
from backend.app.services.notifications import InMemoryOutbox, NotificationService
from backend.app.services.system_control import SystemControl


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
    event = outbox.events["execution:corr-partial:partially_hedged"]
    assert event.event_type == "execution.partially_hedged"
    assert "private_key" not in event.payload


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
