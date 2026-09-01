import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from backend.app.adapters.oddpool.client import OddpoolClient, retry_delay_seconds
from backend.app.adapters.oddpool.schema import OddpoolArbitrageRow, OddpoolResponse
from backend.app.services.discovery import DiscoveryService, InMemoryCandidateStore


def load_fixture() -> dict:
    return json.loads(Path("backend/tests/fixtures/oddpool/opportunities.json").read_text())


def load_official_fixture() -> list[dict[str, object]]:
    return json.loads(
        Path("backend/tests/fixtures/oddpool/arbitrage_current.json").read_text()
    )


@pytest.mark.asyncio
async def test_oddpool_import_is_idempotent_and_keeps_prices_as_evidence_only() -> None:
    response = OddpoolResponse.model_validate(load_fixture())
    store = InMemoryCandidateStore()
    service = DiscoveryService(store)

    first = await service.ingest(response.opportunities)
    second = await service.ingest(response.opportunities)

    assert first.imported == 1
    assert second.imported == 0
    assert len(store.candidates) == 1
    candidate = next(iter(store.candidates.values()))
    assert candidate.source_prices == {"kalshi": "0.76", "polymarket": "0.11"}
    assert not hasattr(candidate, "quote_evaluation")


@pytest.mark.asyncio
async def test_discovery_marks_a_missing_source_link_invalid() -> None:
    response = OddpoolResponse.model_validate(load_fixture())
    opportunity = response.opportunities[0]
    missing_link = opportunity.legs[0].model_copy(update={"market_url": None})
    incomplete = opportunity.model_copy(
        update={"legs": [missing_link, opportunity.legs[1]]}
    )
    store = InMemoryCandidateStore()

    await DiscoveryService(store).ingest([incomplete])

    candidate = next(iter(store.candidates.values()))
    assert candidate.source_urls == {
        "polymarket": "https://polymarket.com/event/example"
    }
    assert candidate.source_link_invalid is True


def test_oddpool_schema_rejects_unknown_venue() -> None:
    payload = load_fixture()
    payload["opportunities"][0]["legs"][0]["venue"] = "unknown"

    with pytest.raises(ValueError):
        OddpoolResponse.model_validate(payload)


@pytest.mark.asyncio
async def test_oddpool_client_uses_official_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.oddpool.com/arbitrage/current")
        assert request.headers["x-api-key"] == "test-token"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=load_official_fixture())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OddpoolClient("test-token", http_client)
        response = await client.fetch_opportunities()

    opportunity = response.opportunities[0]
    assert opportunity.id == "oddpool:42:c25"
    assert opportunity.updated_at == datetime(2026, 3, 11, 14, 30, tzinfo=UTC)
    assert opportunity.resolves_at == datetime(2026, 3, 19, 18, 0, tzinfo=UTC)
    assert opportunity.gross_spread == "0.08"
    assert opportunity.estimated_fees == "0.03"
    assert [
        (leg.venue.value, leg.outcome, leg.market_ref) for leg in opportunity.legs
    ] == [
        ("kalshi", "yes", "KXFEDDECISION-26MAR-C25"),
        ("polymarket", "no", "fed-rate-march"),
    ]


@pytest.mark.asyncio
async def test_oddpool_client_isolates_bad_and_unsupported_rows() -> None:
    valid = load_official_fixture()[0]
    malformed = {"event_id": 99}
    opinion = {
        **valid,
        "event_id": 100,
        "buy_yes_market": "kalshi",
        "buy_no_market": "opinion",
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[valid, malformed, opinion])
        )
    ) as http_client:
        response = await OddpoolClient("test-token", http_client).fetch_opportunities()

    assert [item.id for item in response.opportunities] == ["oddpool:42:c25"]
    assert response.errors == ("row 1: invalid Oddpool arbitrage row",)


@pytest.mark.parametrize(
    "identifier_path",
    [
        "event_id",
        "outcome_key",
        "polymarket_event_slug",
        "kalshi.market_ticker",
        "polymarket.condition_id",
        "polymarket.no_token_id",
    ],
)
@pytest.mark.asyncio
async def test_oddpool_client_rejects_blank_native_identifiers(
    identifier_path: str,
) -> None:
    row = json.loads(json.dumps(load_official_fixture()[0]))
    owner, separator, field = identifier_path.partition(".")
    if separator:
        row[owner][field] = "   "
    else:
        row[owner] = "   "

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[row])
        )
    ) as http_client:
        response = await OddpoolClient("test-token", http_client).fetch_opportunities()

    assert response.opportunities == []
    assert response.errors == ("row 0: invalid Oddpool arbitrage row",)


def test_candidate_id_does_not_change_with_buy_direction() -> None:
    row = load_official_fixture()[0]
    first = OddpoolArbitrageRow.model_validate(row).to_opportunity()
    reversed_row = {
        **row,
        "buy_yes_market": "polymarket",
        "buy_no_market": "kalshi",
    }
    second = OddpoolArbitrageRow.model_validate(reversed_row).to_opportunity()

    assert first is not None
    assert second is not None
    assert first.id == second.id == "oddpool:42:c25"
    assert [leg.outcome for leg in first.legs] == ["yes", "no"]
    assert [leg.outcome for leg in second.legs] == ["no", "yes"]


@pytest.mark.asyncio
async def test_oddpool_client_retries_429_with_bounded_delays() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=load_official_fixture())

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        response = await OddpoolClient(
            "test-token",
            http_client,
            sleeper=record_sleep,
        ).fetch_opportunities()

    assert attempts == 3
    assert delays == [1, 2]
    assert len(response.opportunities) == 1


def test_retry_delay_caps_at_sixty_seconds() -> None:
    assert [retry_delay_seconds(attempt) for attempt in range(7)] == [1, 2, 4, 8, 16, 60, 60]
