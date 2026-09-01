from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from backend.app.adapters import pair_metadata
from backend.app.adapters.oddpool.schema import OddpoolOpportunity
from backend.app.adapters.pair_metadata import (
    NativePairMetadataResolver,
    _decimal_field,
)


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
                    "market_ref": "K-EVENT",
                    "market_url": "https://kalshi.com/markets/K-EVENT",
                    "display_price": "0.70",
                },
                {
                    "venue": "polymarket",
                    "outcome": "yes",
                    "market_ref": "event-slug",
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
async def test_resolver_parses_gamma_json_decimals_exactly() -> None:
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
                        "tick_size": "0.01",
                        "minimum_order_size": "1",
                    }
                },
            )
        return httpx.Response(
            200,
            content=(
                b'[{"question":"Will the event happen?",'
                b'"description":"Polymarket native rule",'
                b'"conditionId":"0xcondition",'
                b'"outcomes":"[\\"Yes\\", \\"No\\"]",'
                b'"clobTokenIds":"[\\"token-yes\\", \\"token-no\\"]",'
                b'"orderMinSize":5,"orderPriceMinTickSize":0.001}]'
            ),
            headers={"content-type": "application/json"},
        )

    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "oddpool:decimal:yes",
            "title": "Will the event happen?",
            "outcome": "yes",
            "updated_at": "2026-09-01T00:00:00Z",
            "gross_spread": "0.08",
            "estimated_fees": "0.03",
            "legs": [
                {
                    "venue": "kalshi",
                    "outcome": "no",
                    "market_ref": "K-EVENT",
                    "display_price": "0.69",
                },
                {
                    "venue": "polymarket",
                    "outcome": "yes",
                    "market_ref": "event-slug",
                    "display_price": "0.32",
                    "source_condition_id": "0xcondition",
                    "source_token_id": "token-yes",
                },
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        pair = await NativePairMetadataResolver(
            kalshi_base_url="https://kalshi.test",
            polymarket_gamma_url="https://gamma.test",
            http_client=http,
        ).resolve(opportunity)

    assert pair.minimum_quantity == Decimal(5)
    assert pair.polymarket_minimum_tick == Decimal("0.001")


@pytest.mark.asyncio
async def test_resolver_derives_kalshi_rule_url_when_native_api_omits_it() -> None:
    ticker = "KXBOXING-26SEP19FMAYMPAC-FMAY"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "kalshi.test":
            return httpx.Response(
                200,
                json={
                    "market": {
                        "ticker": ticker,
                        "title": "Will Floyd Mayweather beat Manny Pacquiao?",
                        "status": "open",
                        "rules_primary": "Resolves yes if Floyd Mayweather wins the bout.",
                        "rules_secondary": "Official results determine the outcome.",
                        "price_level_structure": "linear_cent",
                        "price_ranges": [
                            {
                                "start": "0.0000",
                                "end": "1.0000",
                                "step": "0.0100",
                            }
                        ],
                    }
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "question": "Will Floyd Mayweather beat Manny Pacquiao?",
                    "description": "Polymarket native rule",
                    "conditionId": "0xcondition",
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["token-yes", "token-no"]',
                    "orderMinSize": "1",
                    "orderPriceMinTickSize": "0.01",
                    "active": True,
                }
            ],
        )

    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "oddpool-missing-kalshi-rule-url",
            "title": "Will Floyd Mayweather beat Manny Pacquiao?",
            "outcome": "complementary",
            "updated_at": "2026-09-01T00:00:00Z",
            "gross_spread": "0.08",
            "estimated_fees": "0.03",
            "legs": [
                {
                    "venue": "kalshi",
                    "outcome": "no",
                    "market_ref": ticker,
                    "market_url": None,
                    "display_price": "0.69",
                },
                {
                    "venue": "polymarket",
                    "outcome": "yes",
                    "market_ref": "boxing-event",
                    "market_url": "https://polymarket.com/event/boxing-event",
                    "display_price": "0.32",
                },
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        pair = await NativePairMetadataResolver(
            kalshi_base_url="https://kalshi.test",
            polymarket_gamma_url="https://gamma.test",
            http_client=http,
        ).resolve(opportunity)

    assert pair.kalshi_market_id == ticker
    assert pair.kalshi_rule_text == "Resolves yes if Floyd Mayweather wins the bout."
    assert pair.kalshi_rule_url == f"https://kalshi.com/markets/{ticker}"
    assert pair.kalshi_minimum_tick == Decimal("0.01")
    assert pair.minimum_quantity >= Decimal(1)
    assert pair.quantity_step == Decimal(1)


@pytest.mark.asyncio
async def test_resolver_preserves_native_kalshi_rule_url_over_oddpool_link() -> None:
    native_rule_url = "https://kalshi.com/rules/custom"

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
                        "rules_url": native_rule_url,
                        "price_level_structure": "linear_cent",
                        "price_ranges": [
                            {
                                "start": "0.0000",
                                "end": "1.0000",
                                "step": "0.0100",
                            }
                        ],
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
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["token-yes", "token-no"]',
                    "orderMinSize": "1",
                    "orderPriceMinTickSize": "0.01",
                    "active": True,
                }
            ],
        )

    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "oddpool-native-kalshi-rule-url",
            "title": "Will the event happen?",
            "outcome": "complementary",
            "updated_at": "2026-09-01T00:00:00Z",
            "gross_spread": "0.08",
            "estimated_fees": "0.03",
            "legs": [
                {
                    "venue": "kalshi",
                    "outcome": "no",
                    "market_ref": "K-EVENT",
                    "market_url": "https://third-party.example/old-kalshi-link",
                    "display_price": "0.69",
                },
                {
                    "venue": "polymarket",
                    "outcome": "yes",
                    "market_ref": "event-slug",
                    "market_url": "https://polymarket.com/event/event-slug",
                    "display_price": "0.32",
                },
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        pair = await NativePairMetadataResolver(
            kalshi_base_url="https://kalshi.test",
            polymarket_gamma_url="https://gamma.test",
            http_client=http,
        ).resolve(opportunity)

    assert pair.kalshi_rule_url == native_rule_url


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
            content=(
                b'[{"markets":[{"question":"Will the event happen?",'
                b'"description":"Polymarket native rule",'
                b'"category":"news",'
                b'"outcomes":"[\\"Yes\\", \\"No\\"]",'
                b'"clobTokenIds":"[\\"token-yes\\", \\"token-no\\"]",'
                b'"orderMinSize":5,'
                b'"orderPriceMinTickSize":0.001,'
                b'"endDate":"2026-08-27T16:30:00Z"}]}]'
            ),
            headers={"content-type": "application/json"},
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
                {"venue": "kalshi", "outcome": "no", "market_ref": "K-EVENT", "market_url": "https://kalshi.com/markets/K-EVENT", "display_price": "0.70"},
                {"venue": "polymarket", "outcome": "yes", "market_ref": "event-slug", "market_url": "https://polymarket.com/event/event-slug", "display_price": "0.20"},
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
    assert pair.minimum_quantity == Decimal(5)
    assert pair.polymarket_minimum_tick == Decimal("0.001")
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
                {"venue": "kalshi", "outcome": "no", "market_ref": "K-EVENT", "market_url": "https://kalshi.com/markets/K-EVENT", "display_price": "0.70"},
                {"venue": "polymarket", "outcome": "yes", "market_ref": "event-slug", "market_url": "https://polymarket.com/event/event-slug", "display_price": "0.20"},
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


@pytest.mark.parametrize(
    ("condition_id", "token_id", "message"),
    [
        ("0xwrong", "token-yes", "condition ID must resolve to one market"),
        ("0xcondition", "wrong-token", "Polymarket token ID mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_resolver_rejects_oddpool_native_id_mismatch(
    condition_id: str,
    token_id: str,
    message: str,
) -> None:
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
                        "tick_size": "0.01",
                        "minimum_order_size": "1",
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
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["token-yes", "token-no"]',
                    "orderMinSize": "1",
                    "orderPriceMinTickSize": "0.01",
                }
            ],
        )

    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "oddpool:42:yes",
            "title": "Will the event happen?",
            "outcome": "yes",
            "updated_at": "2026-09-01T00:00:00Z",
            "gross_spread": "0.08",
            "estimated_fees": "0.03",
            "legs": [
                {
                    "venue": "kalshi",
                    "outcome": "no",
                    "market_ref": "K-EVENT",
                    "display_price": "0.69",
                },
                {
                    "venue": "polymarket",
                    "outcome": "yes",
                    "market_ref": "event-slug",
                    "display_price": "0.32",
                    "source_condition_id": condition_id,
                    "source_token_id": token_id,
                },
            ],
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http:
        resolver = NativePairMetadataResolver(
            kalshi_base_url="https://kalshi.test",
            polymarket_gamma_url="https://gamma.test",
            http_client=http,
        )
        with pytest.raises(ValueError, match=message):
            await resolver.resolve(opportunity)


@pytest.mark.parametrize("value", ["0.001", 1, Decimal("0.001")])
def test_polymarket_decimal_field_accepts_exact_positive_values(value: object) -> None:
    assert _decimal_field({"tick": value}, "tick", maximum=Decimal(1)) == Decimal(str(value))


@pytest.mark.parametrize("value", [True, 0.001])
def test_polymarket_decimal_field_rejects_inexact_types(value: object) -> None:
    with pytest.raises(TypeError, match="must be an exact decimal"):
        _decimal_field({"tick": value}, "tick", maximum=Decimal(1))


def test_polymarket_decimal_field_reports_missing_fields_separately() -> None:
    with pytest.raises(TypeError, match="Polymarket metadata is missing tick"):
        _decimal_field({}, "tick", maximum=Decimal(1))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-0.001", "1.001"])
def test_polymarket_tick_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        _decimal_field({"tick": value}, "tick", maximum=Decimal(1))


def test_gamma_payload_rejects_non_standard_numeric_constants() -> None:
    request = httpx.Request("GET", "https://gamma.test/markets")
    response = httpx.Response(
        200,
        text='[{"orderPriceMinTickSize": NaN}]',
        request=request,
    )

    with pytest.raises(ValueError, match="invalid number"):
        pair_metadata._gamma_payload(response)
