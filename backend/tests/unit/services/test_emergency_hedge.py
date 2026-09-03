from decimal import Decimal

import pytest

from backend.app.domain.enums import Venue
from backend.app.services.emergency_hedge import (
    EmergencyAction,
    EmergencyHedgeService,
    InMemoryEmergencyRemediationStore,
    RemediationStatus,
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


def emergency_service(
    ports: dict[Venue, RecordingPort],
) -> EmergencyHedgeService:
    return EmergencyHedgeService(
        ports,
        remediation_store=InMemoryEmergencyRemediationStore(),
    )


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
    service = emergency_service(ports)

    result = await service.resolve(exposure(Decimal("1.50")), Decimal(2))

    assert result.action is EmergencyAction.HEDGE
    assert result.resolved is True
    assert len(ports[Venue.POLYMARKET].requests) == 1
    assert ports[Venue.KALSHI].requests == []


@pytest.mark.asyncio
async def test_closes_filled_leg_when_hedge_loss_exceeds_limit() -> None:
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: RecordingPort()}
    service = emergency_service(ports)

    result = await service.resolve(exposure(Decimal("2.01")), Decimal(2))

    assert result.action is EmergencyAction.CLOSE
    assert len(ports[Venue.KALSHI].requests) == 1
    assert ports[Venue.POLYMARKET].requests == []


@pytest.mark.asyncio
async def test_emergency_action_is_idempotent_per_execution() -> None:
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: RecordingPort()}
    service = emergency_service(ports)

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
    service = emergency_service(ports)

    first = await service.resolve(exposure(Decimal("1.50")), Decimal(2))
    second = await service.resolve(exposure(Decimal("1.50")), Decimal(2))

    assert first is second
    assert first.resolved is True
    assert len(timeout_port.requests) == 1
    assert timeout_port.lookups == 1


@pytest.mark.asyncio
async def test_emergency_query_failure_remains_unknown_without_resubmitting() -> None:
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
    service = emergency_service(ports)

    first = await service.resolve(exposure(Decimal("1.50")), Decimal(2))
    second = await service.resolve(exposure(Decimal("1.50")), Decimal(2))

    assert first.order.status is OrderStatus.UNKNOWN
    assert second.order.status is OrderStatus.UNKNOWN
    assert first.resolved is False
    assert len(unresolved_port.requests) == 1
    assert unresolved_port.lookups == 2


@pytest.mark.asyncio
async def test_emergency_generic_submit_failure_is_queried_before_marking_unknown() -> None:
    class GenericFailurePort(RecordingPort):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0

        async def submit_fok(self, request: OrderRequest) -> OrderSubmissionResult:
            self.requests.append(request)
            raise RuntimeError("connection closed after write")

        async def find_by_client_order_id(
            self,
            client_order_id: str,
        ) -> OrderSubmissionResult | None:
            self.lookups += 1
            return None

    failed_port = GenericFailurePort()
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: failed_port}
    service = emergency_service(ports)

    result = await service.resolve(exposure(Decimal("1.50")), Decimal(2))

    assert result.order.status is OrderStatus.UNKNOWN
    assert result.resolved is False
    assert len(failed_port.requests) == 1
    assert failed_port.lookups == 1


@pytest.mark.asyncio
async def test_started_remediation_is_reconciled_without_a_second_submission() -> None:
    class MissingOrderPort(RecordingPort):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0

        async def find_by_client_order_id(
            self,
            client_order_id: str,
        ) -> OrderSubmissionResult | None:
            self.lookups += 1
            return None

    missing_order_port = MissingOrderPort()
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: missing_order_port}
    remediations = InMemoryEmergencyRemediationStore()
    remediation_key = "execution:corr-1:partially_hedged"
    await remediations.start_remediation(
        remediation_key,
        "corr-1-emergency-hedge",
        Venue.POLYMARKET,
    )
    service = EmergencyHedgeService(ports, remediation_store=remediations)

    result = await service.resolve(
        exposure(Decimal("1.50")),
        Decimal(2),
        remediation_key=remediation_key,
    )

    assert result.order.status is OrderStatus.UNKNOWN
    assert missing_order_port.requests == []
    assert missing_order_port.lookups == 1
    remediation = await remediations.get_remediation(remediation_key)
    assert remediation is not None
    assert remediation.status is RemediationStatus.UNKNOWN


@pytest.mark.asyncio
async def test_unknown_remediation_reconciles_again_when_venue_becomes_consistent() -> None:
    class EventuallyVisiblePort(RecordingPort):
        def __init__(self) -> None:
            super().__init__()
            self.lookups = 0

        async def find_by_client_order_id(
            self,
            client_order_id: str,
        ) -> OrderSubmissionResult | None:
            self.lookups += 1
            if self.lookups == 1:
                return None
            return OrderSubmissionResult(
                client_order_id,
                OrderStatus.FILLED,
                (
                    FillReport(
                        "eventually-visible-fill",
                        Decimal(4),
                        Decimal("0.24"),
                        Decimal("0.01"),
                    ),
                ),
            )

    eventually_visible_port = EventuallyVisiblePort()
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: eventually_visible_port}
    remediations = InMemoryEmergencyRemediationStore()
    remediation_key = "execution:corr-1:partially_hedged"
    await remediations.start_remediation(
        remediation_key,
        "corr-1-emergency-hedge",
        Venue.POLYMARKET,
    )

    first = await EmergencyHedgeService(
        ports,
        remediation_store=remediations,
    ).resolve(exposure(Decimal("1.50")), Decimal(2), remediation_key=remediation_key)
    second = await EmergencyHedgeService(
        ports,
        remediation_store=remediations,
    ).resolve(exposure(Decimal("1.50")), Decimal(2), remediation_key=remediation_key)

    assert first.order.status is OrderStatus.UNKNOWN
    assert second.resolved is True
    assert eventually_visible_port.requests == []
    assert eventually_visible_port.lookups == 2
    remediation = await remediations.get_remediation(remediation_key)
    assert remediation is not None
    assert remediation.status is RemediationStatus.RESOLVED


@pytest.mark.asyncio
async def test_emergency_submission_persists_intent_before_venue_write() -> None:
    remediations = InMemoryEmergencyRemediationStore()
    remediation_key = "execution:corr-1:partially_hedged"

    class IntentCheckingPort(RecordingPort):
        async def submit_fok(self, request: OrderRequest) -> OrderSubmissionResult:
            remediation = await remediations.get_remediation(remediation_key)
            assert remediation is not None
            assert remediation.status is RemediationStatus.STARTED
            return await super().submit_fok(request)

    intent_port = IntentCheckingPort()
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: intent_port}
    service = EmergencyHedgeService(ports, remediation_store=remediations)

    result = await service.resolve(
        exposure(Decimal("1.50")),
        Decimal(2),
        remediation_key=remediation_key,
    )

    assert result.resolved is True
    remediation = await remediations.get_remediation(remediation_key)
    assert remediation is not None
    assert remediation.status is RemediationStatus.RESOLVED


@pytest.mark.asyncio
async def test_emergency_action_submits_and_is_not_simulated() -> None:
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: RecordingPort()}
    service = emergency_service(ports)

    result = await service.resolve(exposure(Decimal("1.50")), Decimal(2))

    assert result.action is EmergencyAction.HEDGE
    assert result.simulated is False
    assert result.resolved is True
    assert len(ports[Venue.POLYMARKET].requests) == 1


@pytest.mark.asyncio
async def test_emergency_action_caches_one_real_result_per_execution() -> None:
    ports = {Venue.KALSHI: RecordingPort(), Venue.POLYMARKET: RecordingPort()}
    service = emergency_service(ports)

    first = await service.resolve(exposure(Decimal("1.50")), Decimal(2))
    second = await service.resolve(exposure(Decimal("1.50")), Decimal(2))

    assert first is second
    assert first.simulated is False
    assert len(ports[Venue.POLYMARKET].requests) == 1
