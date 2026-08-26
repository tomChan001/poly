from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.app.core.config import TradingMode
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
async def test_shadow_partial_fill_records_simulated_incident_and_outbox() -> None:
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
        mode=TradingMode.SHADOW,
        now=datetime(2026, 8, 26, tzinfo=UTC),
    )
    second = await supervisor.finalize(
        record,
        mode=TradingMode.SHADOW,
        now=datetime(2026, 8, 26, tzinfo=UTC),
    )

    assert first is second
    assert first is not None
    assert first.action == "simulate_hedge"
    assert first.simulated is True
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
        mode=TradingMode.LIMITED_AUTO,
        now=datetime(2026, 8, 26, tzinfo=UTC),
    )

    assert incident is None
    assert control.opening_enabled is False
    assert control.reason == "actual fee differs from estimate"
