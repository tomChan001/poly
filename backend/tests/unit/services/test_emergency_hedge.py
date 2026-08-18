from decimal import Decimal

import pytest

from backend.app.domain.enums import Venue
from backend.app.services.emergency_hedge import (
    EmergencyAction,
    EmergencyHedgeService,
    UnhedgedExposure,
)
from backend.app.services.execution import (
    FillReport,
    OrderOutcomeUnknown,
    OrderRequest,
    OrderStatus,
    OrderSubmissionResult,
)


class RecordingPort:
    def __init__(self) -> None:
        self.requests: list[OrderRequest] = []

    async def submit_fok(self, request: OrderRequest) -> OrderSubmissionResult:
        self.requests.append(request)
        client_order_id = request.client_order_id
        quantity = request.quantity
        return OrderSubmissionResult(
            client_order_id,
            OrderStatus.FILLED,
            (FillReport("emergency-fill", quantity, Decimal("0.25"), Decimal("0.01")),),
        )

    async def find_by_client_order_id(self, client_order_id: str) -> OrderSubmissionResult | None:
        return None


def exposure(hedge_loss: Decimal) -> UnhedgedExposure:
    return UnhedgedExposure(
        correlation_id="corr-1",
        quantity=Decimal(4),
        missing_venue=Venue.POLYMARKET,
        missing_market_id="P-MARKET",
        missing_outcome="YES",
        hedge_limit_price=Decimal("0.24"),
        hedge_worst_case_loss=hedge_loss,
        filled_venue=Venue.KALSHI,
        filled_market_id="K-MARKET",
        filled_outcome="NO",
        close_limit_price=Decimal("0.68"),
    )


@pytest.mark.asyncio
async def test_hedges_missing_leg_when_loss_is_within_limit() -> None:
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: RecordingPort()}
    service = EmergencyHedgeService(ports)

    result = await service.resolve(exposure(Decimal("1.50")), Decimal(2))

    assert result.action is EmergencyAction.HEDGE
    assert result.resolved is True
    assert len(ports[Venue.POLYMARKET].requests) == 1
    assert ports[Venue.KALSHI].requests == []


@pytest.mark.asyncio
async def test_closes_filled_leg_when_hedge_loss_exceeds_limit() -> None:
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: RecordingPort()}
    service = EmergencyHedgeService(ports)

    result = await service.resolve(exposure(Decimal("2.01")), Decimal(2))

    assert result.action is EmergencyAction.CLOSE
    assert len(ports[Venue.KALSHI].requests) == 1
    assert ports[Venue.POLYMARKET].requests == []


@pytest.mark.asyncio
async def test_emergency_action_is_idempotent_per_execution() -> None:
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: RecordingPort()}
    service = EmergencyHedgeService(ports)

    first = await service.resolve(exposure(Decimal("1.50")), Decimal(2))
    second = await service.resolve(exposure(Decimal("1.50")), Decimal(2))

    assert first is second
    assert len(ports[Venue.POLYMARKET].requests) == 1


@pytest.mark.asyncio
async def test_emergency_timeout_is_queried_and_never_resubmitted() -> None:
    class TimeoutAfterAcceptingPort(RecordingPort):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0
            self.accepted: OrderSubmissionResult | None = None

        async def submit_fok(self, request: OrderRequest) -> OrderSubmissionResult:
            self.requests.append(request)
            self.accepted = OrderSubmissionResult(
                request.client_order_id,
                OrderStatus.FILLED,
                (
                    FillReport(
                        "accepted-fill",
                        request.quantity,
                        request.limit_price,
                        Decimal("0.01"),
                    ),
                ),
            )
            raise OrderOutcomeUnknown("response timeout")

        async def find_by_client_order_id(
            self,
            client_order_id: str,
        ) -> OrderSubmissionResult | None:
            self.lookups += 1
            return self.accepted

    timeout_port = TimeoutAfterAcceptingPort()
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: timeout_port}
    service = EmergencyHedgeService(ports)

    first = await service.resolve(exposure(Decimal("1.50")), Decimal(2))
    second = await service.resolve(exposure(Decimal("1.50")), Decimal(2))

    assert first is second
    assert first.resolved is True
    assert len(timeout_port.requests) == 1
    assert timeout_port.lookups == 1


@pytest.mark.asyncio
async def test_emergency_query_failure_is_cached_as_unknown() -> None:
    class UnresolvedEmergencyPort(RecordingPort):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0

        async def submit_fok(self, request: OrderRequest) -> OrderSubmissionResult:
            self.requests.append(request)
            raise OrderOutcomeUnknown("response timeout")

        async def find_by_client_order_id(
            self,
            client_order_id: str,
        ) -> OrderSubmissionResult | None:
            self.lookups += 1
            raise RuntimeError("query endpoint unavailable")

    unresolved_port = UnresolvedEmergencyPort()
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: unresolved_port}
    service = EmergencyHedgeService(ports)

    first = await service.resolve(exposure(Decimal("1.50")), Decimal(2))
    second = await service.resolve(exposure(Decimal("1.50")), Decimal(2))

    assert first is second
    assert first.order.status is OrderStatus.UNKNOWN
    assert first.resolved is False
    assert len(unresolved_port.requests) == 1
    assert unresolved_port.lookups == 1
