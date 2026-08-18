from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from backend.app.domain.enums import Venue
from backend.app.services.execution import (
    ExecutionTradingPort,
    OrderAction,
    OrderOutcomeUnknown,
    OrderRequest,
    OrderStatus,
    OrderSubmissionResult,
)


class EmergencyAction(StrEnum):
    HEDGE = "hedge"
    CLOSE = "close"


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


class EmergencyHedgeService:
    def __init__(self, ports: dict[Venue, ExecutionTradingPort]) -> None:
        self._ports = ports
        self._results: dict[str, EmergencyResult] = {}

    async def resolve(
        self,
        exposure: UnhedgedExposure,
        maximum_unhedged_loss: Decimal,
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

        port = self._ports[request.venue]
        try:
            order = await port.submit_fok(request)
        except OrderOutcomeUnknown:
            try:
                recovered = await port.find_by_client_order_id(request.client_order_id)
            except Exception:  # noqa: BLE001 - unavailable query remains UNKNOWN
                recovered = None
            order = (
                OrderSubmissionResult(request.client_order_id, OrderStatus.UNKNOWN, ())
                if recovered is None
                else recovered
            )
        result = EmergencyResult(
            action=action,
            order=order,
            resolved=(
                order.status is OrderStatus.FILLED
                and order.filled_quantity == exposure.quantity
            ),
        )
        # Recording before returning makes retries after an API timeout observe
        # one stable disposition instead of sending a second emergency order.
        self._results[exposure.correlation_id] = result
        return result
