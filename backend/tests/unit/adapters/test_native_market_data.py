from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from backend.app.adapters.native_market_data import NativeMarketDataClient
from backend.app.domain.enums import MappingStatus
from backend.app.services.executable_pairs import ExecutablePair


@pytest.mark.asyncio
async def test_native_market_data_fetches_and_normalizes_both_venue_books() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "kalshi.test":
            return httpx.Response(
                200,
                json={
                    "orderbook": {
                        "yes": [[30, 20]],
                        "no": [[70, 20]],
                    },
                    "sequence": 41,
                },
            )
        return httpx.Response(
            200,
            json={
                "hash": "poly-seq-9",
                "timestamp": "1787104800000",
                "asks": [{"price": "0.20", "size": "20"}],
            },
        )

    pair = ExecutablePair(
        id="pair-1",
        title="Native books",
        kalshi_market_id="K-MARKET",
        kalshi_outcome="no",
        kalshi_rule_text="K rule",
        kalshi_rule_url="https://kalshi.test/rule",
        polymarket_market_id="P-TOKEN",
        polymarket_outcome="yes",
        polymarket_rule_text="P rule",
        polymarket_rule_url="https://poly.test/rule",
        minimum_quantity=Decimal(1),
        quantity_step=Decimal(1),
        enabled=True,
        status=MappingStatus.EXACT,
    )
    now = datetime(2026, 8, 19, 2, 0, tzinfo=UTC)
    receipt_times = iter((now, now.replace(microsecond=600_000)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = NativeMarketDataClient(
            kalshi_base_url="https://kalshi.test",
            polymarket_base_url="https://clob.test",
            http_client=http,
            clock=lambda: next(receipt_times),
        )
        kalshi, polymarket = await client.get_books(pair, now)

    assert requested == [
        "https://kalshi.test/trade-api/v2/markets/K-MARKET/orderbook",
        "https://clob.test/book?token_id=P-TOKEN",
    ]
    assert kalshi.sequence == "41"
    assert kalshi.asks[0].price == Decimal("0.70")
    assert polymarket.sequence == "poly-seq-9"
    assert polymarket.asks[0].price == Decimal("0.20")
    assert polymarket.received_at - kalshi.received_at == timedelta(milliseconds=600)


@pytest.mark.asyncio
async def test_native_market_data_does_not_invent_kalshi_capture_time() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "kalshi.test":
            return httpx.Response(
                200,
                json={"orderbook": {"yes": [[30, 20]], "no": [[70, 20]]}},
            )
        return httpx.Response(
            200,
            json={
                "hash": "poly-seq-9",
                "timestamp": "1787104800000",
                "asks": [{"price": "0.20", "size": "20"}],
            },
        )

    pair = ExecutablePair(
        id="pair-1",
        title="Native books",
        kalshi_market_id="K-MARKET",
        kalshi_outcome="no",
        kalshi_rule_text="K rule",
        kalshi_rule_url="https://kalshi.test/rule",
        polymarket_market_id="P-TOKEN",
        polymarket_outcome="yes",
        polymarket_rule_text="P rule",
        polymarket_rule_url="https://poly.test/rule",
        minimum_quantity=Decimal(1),
        quantity_step=Decimal(1),
        enabled=True,
        status=MappingStatus.EXACT,
    )
    now = datetime(2026, 8, 19, 2, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = NativeMarketDataClient(
            kalshi_base_url="https://kalshi.test",
            polymarket_base_url="https://clob.test",
            http_client=http,
        )
        kalshi, _ = await client.get_books(pair, now)

    assert kalshi.captured_at is None
