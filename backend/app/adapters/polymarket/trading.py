from decimal import Decimal
from typing import Protocol

from backend.app.core.decimal import parse_decimal, parse_price
from backend.app.services.execution import (
    FillReport,
    OrderOutcomeUnknown,
    OrderRequest,
    OrderStatus,
    OrderSubmissionResult,
)


class PolymarketOrderTransport(Protocol):
    async def create_order(self, payload: dict[str, object]) -> dict[str, object]: ...

    async def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> dict[str, object] | None: ...


class PolymarketTradingAdapter:
    def __init__(self, transport: PolymarketOrderTransport) -> None:
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

    @staticmethod
    def _payload(request: OrderRequest) -> dict[str, object]:
        # CLOB signing must receive the exact decimal text. Converting either
        # field to float can alter the signed order and its affordability.
        return {
            "token_id": request.market_id,
            "clientOrderId": request.client_order_id,
            "side": request.action.value.upper(),
            "price": str(request.limit_price),
            "size": str(request.quantity),
            "order_type": "FOK",
        }

    @staticmethod
    def _parse(response: dict[str, object], fallback_client_id: str) -> OrderSubmissionResult:
        client_id = response.get("clientOrderId", fallback_client_id)
        raw_status = response.get("status")
        raw_fills = response.get("fills", [])
        if not isinstance(client_id, str) or not isinstance(raw_status, str):
            raise TypeError("Polymarket order identifiers and status must be strings")
        if not isinstance(raw_fills, list):
            raise TypeError("Polymarket fills must be a list")

        fills: list[FillReport] = []
        for raw_fill in raw_fills:
            if not isinstance(raw_fill, dict):
                raise TypeError("Polymarket fill must be an object")
            fill_id = raw_fill.get("id")
            if not isinstance(fill_id, str):
                raise TypeError("Polymarket fill ID must be a string")
            fills.append(
                FillReport(
                    fill_id,
                    _decimal_value(raw_fill.get("size")),
                    _price_value(raw_fill.get("price")),
                    _decimal_value(raw_fill.get("fee", 0)),
                ),
            )

        status = {
            "FILLED": OrderStatus.FILLED,
            "MATCHED": OrderStatus.FILLED,
            "PARTIAL": OrderStatus.PARTIAL,
            "REJECTED": OrderStatus.REJECTED,
            "CANCELED": OrderStatus.PARTIAL if fills else OrderStatus.REJECTED,
        }.get(raw_status.upper(), OrderStatus.UNKNOWN)
        return OrderSubmissionResult(client_id, status, tuple(fills))


def _decimal_value(value: object) -> Decimal:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise TypeError("Polymarket numeric fields must be decimal strings or integers")
    return parse_decimal(value)


def _price_value(value: object) -> Decimal:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise TypeError("Polymarket price must be a decimal string or integer")
    return parse_price(value)
