from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from backend.app.adapters.oddpool.schema import OddpoolOpportunity
from backend.app.adapters.pair_metadata import NativePairMetadataResolver


@pytest.mark.asyncio
async def test_resolver_uses_native_metadata_and_selects_polymarket_outcome_token() -> None:
    requested: list[str] = []
    kalshi_settlement = datetime(2026, 8, 25, 15, 0, tzinfo=UTC)
    polymarket_settlement = datetime(2026, 8, 27, 16, 30, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "kalshi.test":
            return httpx.Response(
                200,
                json={
                    "market": {
                        "ticker": "K-EVENT",
                        "title": "Will the event happen?",
                        "status": "open",
                        "rules_primary": "Kalshi native rule",
                        "rules_url": "https://kalshi.com/markets/K-EVENT",
                        "category": "politics",
                        "tick_size": "0.01",
                        "minimum_order_size": "1",
                        "expiration_time": kalshi_settlement.isoformat(),
                    }
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "question": "Will the event happen?",
                    "description": "Polymarket native rule",
                    "conditionId": "0xcondition",
                    "category": "news",
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["token-yes", "token-no"]',
                    "orderMinSize": "5",
                    "orderPriceMinTickSize": "0.001",
                    "endDate": polymarket_settlement.isoformat(),
                    "active": True,
                }
            ],
        )

    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "oddpool-001",
            "title": "Will the event happen?",
            "outcome": "complementary",
            "updated_at": datetime(2026, 8, 21, 2, 0, tzinfo=UTC).isoformat(),
            "gross_spread": "0.10",
            "estimated_fees": "0.01",
            "legs": [
                {
                    "venue": "kalshi",
                    "outcome": "no",
                    "market_url": "https://kalshi.com/markets/K-EVENT",
                    "display_price": "0.70",
                },
                {
                    "venue": "polymarket",
                    "outcome": "yes",
                    "market_url": "https://polymarket.com/event/event-slug",
                    "display_price": "0.20",
                },
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        resolver = NativePairMetadataResolver(
            kalshi_base_url="https://kalshi.test",
            polymarket_gamma_url="https://gamma.test",
            http_client=http,
        )
        pair = await resolver.resolve(opportunity)

    assert requested == [
        "https://kalshi.test/trade-api/v2/markets/K-EVENT",
        "https://gamma.test/markets?slug=event-slug",
    ]
    assert pair.kalshi_market_id == "K-EVENT"
    assert pair.polymarket_market_id == "token-yes"
    assert pair.kalshi_rule_text == "Kalshi native rule"
    assert pair.polymarket_rule_text == "Polymarket native rule"
    assert pair.kalshi_expected_settlement_at == kalshi_settlement
    assert pair.polymarket_expected_settlement_at == polymarket_settlement
    assert pair.worst_case_settlement_at == polymarket_settlement
    assert pair.kalshi_category == "politics"
    assert pair.polymarket_category == "news"
    assert pair.kalshi_minimum_tick == Decimal("0.01")
    assert pair.polymarket_minimum_tick == Decimal("0.001")
    assert pair.native_fingerprint
    assert pair.material_fingerprint
    assert pair.minimum_quantity == Decimal(5)
    assert pair.quantity_step == Decimal(1)


@pytest.mark.asyncio
async def test_resolver_falls_back_from_market_slug_to_event_slug() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "kalshi.test":
            return httpx.Response(
                200,
                json={
                    "market": {
                        "ticker": "K-EVENT",
                        "title": "Will the event happen?",
                        "status": "open",
                        "rules_primary": "Kalshi native rule",
                        "rules_url": "https://kalshi.com/markets/K-EVENT",
                        "category": "politics",
                        "tick_size": "0.01",
                        "minimum_order_size": "1",
                        "expiration_time": "2026-08-25T15:00:00Z",
                    }
                },
            )
        if request.url.path == "/markets":
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {
                    "markets": [
                        {
                            "question": "Will the event happen?",
                            "description": "Polymarket native rule",
                            "category": "news",
                            "outcomes": '["Yes", "No"]',
                            "clobTokenIds": '["token-yes", "token-no"]',
                            "orderMinSize": "5",
                            "orderPriceMinTickSize": "0.001",
                            "endDate": "2026-08-27T16:30:00Z",
                        }
                    ]
                }
            ],
        )

    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "oddpool-event",
            "title": "Will the event happen?",
            "outcome": "complementary",
            "updated_at": "2026-08-21T02:00:00Z",
            "gross_spread": "0.10",
            "estimated_fees": "0.01",
            "legs": [
                {"venue": "kalshi", "outcome": "no", "market_url": "https://kalshi.com/markets/K-EVENT", "display_price": "0.70"},
                {"venue": "polymarket", "outcome": "yes", "market_url": "https://polymarket.com/event/event-slug", "display_price": "0.20"},
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        pair = await NativePairMetadataResolver(
            kalshi_base_url="https://kalshi.test",
            polymarket_gamma_url="https://gamma.test",
            http_client=http,
        ).resolve(opportunity)

    assert pair.polymarket_market_id == "token-yes"
    assert pair.worst_case_settlement_at == datetime(2026, 8, 27, 16, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_resolver_normalizes_offset_datetimes_to_utc() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "kalshi.test":
            return httpx.Response(
                200,
                json={
                    "market": {
                        "ticker": "K-EVENT",
                        "title": "Will the event happen?",
                        "status": "open",
                        "rules_primary": "Kalshi native rule",
                        "rules_url": "https://kalshi.com/markets/K-EVENT",
                        "category": "politics",
                        "tick_size": "0.01",
                        "minimum_order_size": "1",
                        "expiration_time": "2026-08-25T23:00:00+08:00",
                    }
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "question": "Will the event happen?",
                    "description": "Polymarket native rule",
                    "conditionId": "0xcondition",
                    "category": "news",
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["token-yes", "token-no"]',
                    "orderMinSize": "5",
                    "orderPriceMinTickSize": "0.001",
                    "endDate": "2026-08-25T15:00:00Z",
                    "active": True,
                }
            ],
        )

    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "oddpool-utc",
            "title": "Will the event happen?",
            "outcome": "complementary",
            "updated_at": "2026-08-21T02:00:00Z",
            "gross_spread": "0.10",
            "estimated_fees": "0.01",
            "legs": [
                {"venue": "kalshi", "outcome": "no", "market_url": "https://kalshi.com/markets/K-EVENT", "display_price": "0.70"},
                {"venue": "polymarket", "outcome": "yes", "market_url": "https://polymarket.com/event/event-slug", "display_price": "0.20"},
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        pair = await NativePairMetadataResolver(
            kalshi_base_url="https://kalshi.test",
            polymarket_gamma_url="https://gamma.test",
            http_client=http,
        ).resolve(opportunity)

    assert pair.kalshi_expected_settlement_at == datetime(2026, 8, 25, 15, 0, tzinfo=UTC)
    assert pair.polymarket_expected_settlement_at == datetime(2026, 8, 25, 15, 0, tzinfo=UTC)
    assert pair.worst_case_settlement_at == datetime(2026, 8, 25, 15, 0, tzinfo=UTC)
