import json
from pathlib import Path

import httpx
import pytest

from backend.app.adapters.oddpool.client import OddpoolClient, retry_delay_seconds
from backend.app.adapters.oddpool.schema import OddpoolResponse
from backend.app.services.discovery import DiscoveryService, InMemoryCandidateStore


def load_fixture() -> dict:
    return json.loads(Path("backend/tests/fixtures/oddpool/opportunities.json").read_text())


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


def test_oddpool_schema_rejects_unknown_venue() -> None:
    payload = load_fixture()
    payload["opportunities"][0]["legs"][0]["venue"] = "unknown"

    with pytest.raises(ValueError):
        OddpoolResponse.model_validate(payload)


@pytest.mark.asyncio
async def test_oddpool_client_sends_token_and_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json=load_fixture())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OddpoolClient("https://oddpool.test", "test-token", http_client)
        response = await client.fetch_opportunities()

    assert response.opportunities[0].id == "oddpool-001"


def test_retry_delay_caps_at_sixty_seconds() -> None:
    assert [retry_delay_seconds(attempt) for attempt in range(7)] == [1, 2, 4, 8, 16, 60, 60]
