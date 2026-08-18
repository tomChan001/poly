from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.core.config import TradingMode
from backend.app.domain.enums import ExecutionState, MappingStatus, Venue
from backend.app.services.execution import (
    ControlledExecutionService,
    ExecutionAuthorizationService,
    ExecutionEvidence,
    ExecutionRecord,
    FillReport,
    OrderOutcomeUnknown,
    OrderStatus,
    OrderSubmissionResult,
)
from backend.app.services.system_control import SystemControl

NOW = datetime(2026, 8, 18, 3, 0, tzinfo=UTC)


def evidence() -> ExecutionEvidence:
    return ExecutionEvidence(
        quote_evaluation_id="quote-faults",
        rule_versions=("k-rule", "p-rule"),
        book_sequences=("k-book", "p-book"),
        balance_versions=("k-balance", "p-balance"),
        risk_policy_version="risk-1",
        capital_reservation_id="reserve-faults",
        quantity=Decimal(10),
        kalshi_market_id="K",
        polymarket_market_id="P",
        kalshi_outcome="NO",
        polymarket_outcome="YES",
        kalshi_limit_price=Decimal("0.70"),
        polymarket_limit_price=Decimal("0.20"),
        conservative_roi=Decimal("0.08"),
        minimum_roi=Decimal("0.03"),
    )


class FaultPort:
    def __init__(
        self,
        result: OrderSubmissionResult,
        *,
        timeout_after_accepting: bool = False,
    ) -> None:
        self.result = result
        self.timeout_after_accepting = timeout_after_accepting
        self.submissions = 0
        self.lookups = 0

    async def submit_fok(self, request: object) -> OrderSubmissionResult:
        self.submissions += 1
        if self.timeout_after_accepting:
            raise OrderOutcomeUnknown("response timed out")
        return self.result

    async def find_by_client_order_id(self, client_order_id: str) -> OrderSubmissionResult | None:
        self.lookups += 1
        return self.result


def result(venue: Venue, quantities: tuple[Decimal, ...]) -> OrderSubmissionResult:
    fills = tuple(
        FillReport(f"{venue}-fill-{index}", quantity, Decimal("0.50"), Decimal("0.01"))
        for index, quantity in enumerate(quantities)
    )
    status = OrderStatus.FILLED if sum(quantities, Decimal(0)) == Decimal(10) else OrderStatus.PARTIAL
    return OrderSubmissionResult(f"client-{venue}", status, fills)


@pytest.mark.asyncio
async def test_unknown_response_is_recovered_without_resubmission() -> None:
    kalshi = FaultPort(result(Venue.KALSHI, (Decimal(10),)), timeout_after_accepting=True)
    polymarket = FaultPort(result(Venue.POLYMARKET, (Decimal(10),)))
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(
        {Venue.KALSHI: kalshi, Venue.POLYMARKET: polymarket},
        SystemControl(opening_enabled=True),
        TradingMode.LIMITED_AUTO,
    )

    execution = await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert execution.state is ExecutionState.PAIRED
    assert kalshi.submissions == 1
    assert kalshi.lookups == 1


@pytest.mark.asyncio
async def test_unclassified_transport_failure_also_queries_before_deciding() -> None:
    class FailedResponsePort(FaultPort):
        async def submit_fok(self, request: object) -> OrderSubmissionResult:
            self.submissions += 1
            raise RuntimeError("connection closed after write")

    kalshi = FailedResponsePort(result(Venue.KALSHI, (Decimal(10),)))
    polymarket = FaultPort(result(Venue.POLYMARKET, (Decimal(10),)))
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(
        {Venue.KALSHI: kalshi, Venue.POLYMARKET: polymarket},
        SystemControl(opening_enabled=True),
        TradingMode.LIMITED_AUTO,
    )

    execution = await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert execution.state is ExecutionState.PAIRED
    assert kalshi.submissions == 1
    assert kalshi.lookups == 1


@pytest.mark.asyncio
async def test_unresolved_order_outcome_disables_new_opening() -> None:
    class UnresolvedPort(FaultPort):
        async def submit_fok(self, request: object) -> OrderSubmissionResult:
            self.submissions += 1
            raise OrderOutcomeUnknown("no response")

        async def find_by_client_order_id(
            self,
            client_order_id: str,
        ) -> OrderSubmissionResult | None:
            self.lookups += 1
            raise RuntimeError("reconciliation endpoint unavailable")

    control = SystemControl(opening_enabled=True)
    ports = {
        Venue.KALSHI: UnresolvedPort(result(Venue.KALSHI, ())),
        Venue.POLYMARKET: UnresolvedPort(result(Venue.POLYMARKET, ())),
    }
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(ports, control, TradingMode.LIMITED_AUTO)

    execution = await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert execution.state is ExecutionState.EXCEPTION
    assert control.opening_enabled is False
    assert control.reason == "execution outcome unresolved"


@pytest.mark.asyncio
async def test_duplicate_fill_reports_do_not_inflate_matched_quantity() -> None:
    duplicate = FillReport("same-fill", Decimal(10), Decimal("0.50"), Decimal("0.01"))
    kalshi_result = OrderSubmissionResult("client-k", OrderStatus.FILLED, (duplicate, duplicate))
    ports = {
        Venue.KALSHI: FaultPort(kalshi_result),
        Venue.POLYMARKET: FaultPort(result(Venue.POLYMARKET, (Decimal(10),))),
    }
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(
        ports,
        SystemControl(opening_enabled=True),
        TradingMode.LIMITED_AUTO,
    )

    execution = await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert execution.state is ExecutionState.PAIRED
    assert execution.matched_quantity == Decimal(10)


@pytest.mark.asyncio
async def test_partial_leg_uses_minimum_filled_quantity() -> None:
    ports = {
        Venue.KALSHI: FaultPort(result(Venue.KALSHI, (Decimal(6),))),
        Venue.POLYMARKET: FaultPort(result(Venue.POLYMARKET, (Decimal(10),))),
    }
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(
        ports,
        SystemControl(opening_enabled=True),
        TradingMode.LIMITED_AUTO,
    )

    execution = await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert execution.state is ExecutionState.PARTIALLY_HEDGED
    assert execution.matched_quantity == Decimal(6)
    assert execution.unhedged_quantity == Decimal(4)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kalshi_quantities", "polymarket_quantities", "expected_state"),
    [
        ((), (Decimal(10),), ExecutionState.PARTIALLY_HEDGED),
        ((), (), ExecutionState.EXCEPTION),
    ],
)
async def test_rejection_matrix(
    kalshi_quantities: tuple[Decimal, ...],
    polymarket_quantities: tuple[Decimal, ...],
    expected_state: ExecutionState,
) -> None:
    ports = {
        Venue.KALSHI: FaultPort(result(Venue.KALSHI, kalshi_quantities)),
        Venue.POLYMARKET: FaultPort(result(Venue.POLYMARKET, polymarket_quantities)),
    }
    authorization = ExecutionAuthorizationService().issue(MappingStatus.EXACT, evidence(), NOW)
    service = ControlledExecutionService(
        ports,
        SystemControl(opening_enabled=True),
        TradingMode.LIMITED_AUTO,
    )

    execution = await service.execute(authorization, evidence(), NOW + timedelta(seconds=1))

    assert execution.state is expected_state


@pytest.mark.asyncio
async def test_submitted_execution_recovers_after_process_restart_without_resubmission() -> None:
    ports = {
        Venue.KALSHI: FaultPort(result(Venue.KALSHI, (Decimal(10),))),
        Venue.POLYMARKET: FaultPort(result(Venue.POLYMARKET, (Decimal(10),))),
    }
    service = ControlledExecutionService(
        ports,
        SystemControl(opening_enabled=True),
        TradingMode.LIMITED_AUTO,
    )
    submitted = ExecutionRecord(
        correlation_id="crashed-correlation",
        state=ExecutionState.SUBMITTED,
        requested_quantity=Decimal(10),
    )

    recovered = await service.recover_submitted(submitted, NOW + timedelta(seconds=1))

    assert recovered.state is ExecutionState.PAIRED
    assert all(port.submissions == 0 for port in ports.values())
    assert all(port.lookups == 1 for port in ports.values())
