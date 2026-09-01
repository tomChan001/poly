from decimal import Decimal
from typing import Protocol

from backend.app.core.decimal import parse_decimal
from backend.app.services.execution import (
    FillReport,
    OrderOutcomeUnknown,
    OrderRequest,
    OrderStatus,
    OrderSubmissionResult,
)


class KalshiOrderTransport(Protocol):
    async def create_order(self, payload: dict[str, object]) -> dict[str, object]: ...

    async def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> dict[str, object] | None: ...

    async def get_available_balance(self) -> Decimal: ...


class KalshiTradingAdapter:
    def __init__(self, transport: KalshiOrderTransport) -> None:
        self._transport = transport

    async def submit_fok(self, request: OrderRequest) -> OrderSubmissionResult:
        try:
            response = await self._transport.create_order(self._payload(request))
        except TimeoutError as exc:
            raise OrderOutcomeUnknown(str(exc)) from exc
        return self._parse(response, request.client_order_id)

    async def find_by_client_order_id(
        self,
        client_order_id: str,
    ) -> OrderSubmissionResult | None:
        response = await self._transport.get_order_by_client_id(client_order_id)
        if response is None:
            return None
        return self._parse(response, client_order_id)

    async def get_available_balance(self) -> Decimal:
        return await self._transport.get_available_balance()

    @staticmethod
    def _payload(request: OrderRequest) -> dict[str, object]:
        count = request.quantity.to_integral_value()
        if count != request.quantity:
            raise ValueError("Kalshi order quantity must be a whole contract count")
        price_cents = request.limit_price * Decimal(100)
        if price_cents != price_cents.to_integral_value():
            raise ValueError("Kalshi limit price must resolve to an integer cent")

        side = request.outcome.lower()
        if side not in {"yes", "no"}:
            raise ValueError("Kalshi outcome must be YES or NO")
        # Kalshi represents the selected outcome price in whole cents, unlike
        # Polymarket's decimal token price. Keeping the conversion here stops
        # platform units from leaking into the execution service.
        return {
            "ticker": request.market_id,
            "client_order_id": request.client_order_id,
            "type": "limit",
            "action": request.action.value,
            "side": side,
            "count": int(count),
            f"{side}_price": int(price_cents),
            "time_in_force": "fill_or_kill",
        }

    @staticmethod
    def _parse(response: dict[str, object], fallback_client_id: str) -> OrderSubmissionResult:
        raw_order = response.get("order")
        if not isinstance(raw_order, dict):
            raise TypeError("Kalshi response is missing order data")
        client_id = raw_order.get("client_order_id", fallback_client_id)
        order_id = raw_order.get("order_id")
        raw_status = raw_order.get("status")
        if not isinstance(client_id, str) or not isinstance(order_id, str):
            raise TypeError("Kalshi order identifiers must be strings")
        if not isinstance(raw_status, str):
            raise TypeError("Kalshi order status must be a string")

        quantity = _decimal_value(raw_order.get("fill_count", 0))
        fees = _decimal_value(raw_order.get("fees", 0))
        price_cents = _decimal_value(raw_order.get("average_fill_price", 0))
        fills: tuple[FillReport, ...] = ()
        if quantity > 0:
            fills = (FillReport(order_id, quantity, price_cents / Decimal(100), fees),)
        status = {
            "executed": OrderStatus.FILLED,
            "filled": OrderStatus.FILLED,
            "partially_filled": OrderStatus.PARTIAL,
            "canceled": OrderStatus.PARTIAL if quantity > 0 else OrderStatus.REJECTED,
            "rejected": OrderStatus.REJECTED,
        }.get(raw_status.lower(), OrderStatus.UNKNOWN)
        return OrderSubmissionResult(client_id, status, fills)


def _decimal_value(value: object) -> Decimal:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise TypeError("Kalshi numeric fields must be decimal strings or integers")
    return parse_decimal(value)
