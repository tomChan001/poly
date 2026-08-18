from decimal import Decimal

import pytest

from backend.app.adapters.kalshi.trading import KalshiTradingAdapter
from backend.app.adapters.polymarket.trading import PolymarketTradingAdapter
from backend.app.domain.enums import Venue
from backend.app.services.execution import (
    OrderAction,
    OrderOutcomeUnknown,
    OrderRequest,
    OrderStatus,
)


class KalshiTransport:
    def __init__(self) -> None:
        self.payload: dict[str, object] | None = None

    async def create_order(self, payload: dict[str, object]) -> dict[str, object]:
        self.payload = payload
        return {
            "order": {
                "order_id": "kalshi-order-1",
                "client_order_id": payload["client_order_id"],
                "status": "executed",
                "fill_count": 10,
                "average_fill_price": 70,
                "fees": "0.07",
            },
        }

    async def get_order_by_client_id(self, client_order_id: str) -> dict[str, object] | None:
        return None


class PolymarketTransport:
    def __init__(self) -> None:
        self.payload: dict[str, object] | None = None

    async def create_order(self, payload: dict[str, object]) -> dict[str, object]:
        self.payload = payload
        return {
            "clientOrderId": payload["clientOrderId"],
            "status": "FILLED",
            "fills": [{"id": "poly-fill-1", "size": "10", "price": "0.20", "fee": "0.02"}],
        }

    async def get_order_by_client_id(self, client_order_id: str) -> dict[str, object] | None:
        return None


@pytest.mark.asyncio
async def test_kalshi_uses_integer_cents_and_contracts() -> None:
    transport = KalshiTransport()
    adapter = KalshiTradingAdapter(transport)
    request = OrderRequest(
        Venue.KALSHI,
        "corr-kalshi",
        "K-MARKET",
        "NO",
        Decimal(10),
        Decimal("0.70"),
        OrderAction.BUY,
    )

    result = await adapter.submit_fok(request)

    assert transport.payload == {
        "ticker": "K-MARKET",
        "client_order_id": "corr-kalshi",
        "type": "limit",
        "action": "buy",
        "side": "no",
        "count": 10,
        "no_price": 70,
        "time_in_force": "fill_or_kill",
    }
    assert result.status is OrderStatus.FILLED
    assert result.filled_quantity == Decimal(10)


@pytest.mark.asyncio
async def test_polymarket_keeps_price_and_size_as_decimal_strings() -> None:
    transport = PolymarketTransport()
    adapter = PolymarketTradingAdapter(transport)
    request = OrderRequest(
        Venue.POLYMARKET,
        "corr-poly",
        "token-1",
        "YES",
        Decimal("10.5"),
        Decimal("0.205"),
    )

    result = await adapter.submit_fok(request)

    assert transport.payload == {
        "token_id": "token-1",
        "clientOrderId": "corr-poly",
        "side": "BUY",
        "price": "0.205",
        "size": "10.5",
        "order_type": "FOK",
    }
    assert result.fills[0].price == Decimal("0.20")


@pytest.mark.asyncio
async def test_transport_timeout_becomes_unknown_order_outcome() -> None:
    class TimeoutTransport(PolymarketTransport):
        async def create_order(self, payload: dict[str, object]) -> dict[str, object]:
            raise TimeoutError("socket timed out")

    adapter = PolymarketTradingAdapter(TimeoutTransport())
    request = OrderRequest(
        Venue.POLYMARKET,
        "corr-timeout",
        "token-1",
        "YES",
        Decimal(10),
        Decimal("0.20"),
    )

    with pytest.raises(OrderOutcomeUnknown, match="socket timed out"):
        await adapter.submit_fok(request)
