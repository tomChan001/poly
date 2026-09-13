from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from backend.app.adapters.native_fees import NativePreviewFeeProvider
from backend.app.domain.enums import MappingStatus
from backend.app.domain.market import BookLevel
from backend.app.services.executable_pairs import ExecutablePair
from backend.app.services.optimizer import QuotePolicy


def _provider(http):
    return NativePreviewFeeProvider(
        kalshi_base_url="https://kalshi.test",
        polymarket_base_url="https://clob.test",
        http_client=http,
    )


def _pair():
    return ExecutablePair(
        id="pair-1", title="Native fees", kalshi_market_id="K-MARKET",
        kalshi_outcome="no", kalshi_rule_text="K rule",
        kalshi_rule_url="https://kalshi.test/rule", polymarket_market_id="P-TOKEN",
        polymarket_outcome="yes", polymarket_rule_text="P rule",
        polymarket_rule_url="https://clob.test/rule", minimum_quantity=Decimal(1),
        quantity_step=Decimal(1), enabled=False, status=MappingStatus.PENDING_REVIEW,
        kalshi_category="same-category", polymarket_category="same-category",
    )


def _responses():
    return {
        "/trade-api/v2/markets/K-MARKET": {
            "market": {"ticker": "K-MARKET", "event_ticker": "K-EVENT"}
        },
        "/trade-api/v2/events/K-EVENT": {
            "event": {"event_ticker": "K-EVENT", "series_ticker": "K-SERIES"}
        },
        "/trade-api/v2/series/K-SERIES": {
            "series": {"ticker": "K-SERIES", "fee_type": "quadratic", "fee_multiplier": 1}
        },
        "/markets-by-token/P-TOKEN": {"condition_id": "P-CONDITION"},
        "/clob-markets/P-CONDITION": {
            "fd": {"r": 0.03, "e": 1, "to": True},
            "t": [{"t": "P-TOKEN", "o": "Yes"}],
        },
    }


def _quote(optimizer, *, kalshi_price="0.3"):
    result = optimizer.optimize(
        mapping_status=MappingStatus.EXACT,
        kalshi_category="same-category", polymarket_category="same-category",
        kalshi_asks=[BookLevel(Decimal(kalshi_price), Decimal(1))],
        polymarket_asks=[BookLevel(Decimal("0.2"), Decimal(1))],
        policy=QuotePolicy(
            minimum_roi=Decimal(0), maximum_quantity=Decimal(1), quantity_step=Decimal(1),
            explicit_cost=Decimal(0), risk_buffer=Decimal(0), kalshi_balance=Decimal(100),
            polymarket_balance=Decimal(100), per_trade_limit=Decimal(100),
            per_event_limit=Decimal(100), portfolio_limit=Decimal(100),
        ),
    )
    assert result.best_quote is not None
    return result.best_quote


@pytest.mark.asyncio
async def test_native_fee_provider_uses_public_market_specific_rates() -> None:
    responses = _responses()
    requested = []

    def handler(request):
        assert request.method == "GET"
        requested.append(request.url.path)
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        optimizer = await _provider(http).optimizer_for(_pair())

    quote = _quote(optimizer)
    assert set(requested) == set(responses)
    assert quote.kalshi_fee == Decimal("0.02")
    assert quote.polymarket_fee == Decimal("0.0048")
    assert quote.deployed_capital == Decimal("0.5248")


@pytest.mark.asyncio
async def test_native_fee_provider_honors_event_overrides_and_keeps_snapshots_isolated() -> None:
    responses = _responses()
    event = responses["/trade-api/v2/events/K-EVENT"]["event"]
    event.update(fee_type_override="quadratic_with_maker_fees", fee_multiplier_override=2)

    def handler(request):
        return httpx.Response(200, json=deepcopy(responses[request.url.path]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = _provider(http)
        first = await provider.optimizer_for(_pair())
        event["fee_multiplier_override"] = 0
        responses["/clob-markets/P-CONDITION"]["fd"]["r"] = 0
        second = await provider.optimizer_for(replace(_pair(), id="pair-2"))

    assert _quote(first).kalshi_fee == Decimal("0.03")
    assert _quote(first).polymarket_fee == Decimal("0.0048")
    assert _quote(second).kalshi_fee == 0
    assert _quote(second).polymarket_fee == 0


@pytest.mark.asyncio
async def test_native_kalshi_fee_estimate_accounts_for_subcent_position_cost() -> None:
    responses = _responses()

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=responses[request.url.path])
    )) as http:
        optimizer = await _provider(http).optimizer_for(_pair())

    # Trade fee 0.0037, then balance alignment rounds 0.055 + 0.0037 up to 0.06.
    assert _quote(optimizer, kalshi_price="0.055").kalshi_fee == Decimal("0.005")


@pytest.mark.asyncio
@pytest.mark.parametrize("location, field, value", [
    ("series", "fee_type", "flat"),
    ("series", "fee_multiplier", None),
    ("series", "fee_multiplier", -1),
    ("series", "fee_multiplier", "NaN"),
    ("event", "fee_multiplier_override", "Infinity"),
    ("event", "fee_type_override", "unrecognized"),
    ("fd", "r", None),
    ("fd", "r", True),
    ("fd", "r", -1),
    ("fd", "r", "Infinity"),
    ("fd", "e", 2),
    ("fd", "e", None),
    ("fd", "to", None),
])
async def test_unknown_fee_metadata_is_rejected(location, field, value) -> None:
    responses = _responses()
    target = {
        "series": responses["/trade-api/v2/series/K-SERIES"]["series"],
        "event": responses["/trade-api/v2/events/K-EVENT"]["event"],
        "fd": responses["/clob-markets/P-CONDITION"]["fd"],
    }[location]
    target[field] = value

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=responses[request.url.path])
    )) as http:
        with pytest.raises(ValueError, match="fee"):
            await _provider(http).optimizer_for(_pair())


@pytest.mark.asyncio
async def test_explicit_zero_polymarket_fee_accepts_zero_exponent() -> None:
    responses = _responses()
    responses["/clob-markets/P-CONDITION"]["fd"] = {"r": 0, "e": 0, "to": True}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=responses[request.url.path])
    )) as http:
        optimizer = await _provider(http).optimizer_for(_pair())

    assert _quote(optimizer).polymarket_fee == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path, payload", [
    ("/trade-api/v2/markets/K-MARKET", {}),
    ("/trade-api/v2/events/K-EVENT", {"event": {}}),
    ("/markets-by-token/P-TOKEN", {"condition_id": ""}),
    ("/clob-markets/P-CONDITION", {"t": [{"t": "P-TOKEN"}]}),
    ("/clob-markets/P-CONDITION", []),
])
async def test_incomplete_fee_responses_are_unknown(path, payload) -> None:
    responses = _responses()
    responses[path] = payload
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=responses[request.url.path])
    )) as http:
        with pytest.raises(ValueError, match="fee"):
            await _provider(http).optimizer_for(_pair())


@pytest.mark.asyncio
async def test_native_fee_provider_rejects_wrong_token_and_http_failures() -> None:
    responses = _responses()
    responses["/clob-markets/P-CONDITION"]["t"] = [{"t": "OTHER-TOKEN", "o": "Yes"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=responses[request.url.path])
    )) as http:
        with pytest.raises(ValueError, match="fee"):
            await _provider(http).optimizer_for(_pair())

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(503)
    )) as http:
        with pytest.raises(ValueError, match="fee"):
            await _provider(http).optimizer_for(_pair())
