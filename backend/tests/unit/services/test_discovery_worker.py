import json
from pathlib import Path

import pytest

from backend.app.adapters.oddpool.schema import OddpoolResponse
from backend.app.services.discovery import DiscoveryService, InMemoryCandidateStore
from backend.app.workers.discovery import DiscoveryWorker


class FixtureClient:
    async def fetch_opportunities(self) -> OddpoolResponse:
        payload = json.loads(
            Path("backend/tests/fixtures/oddpool/opportunities.json").read_text()
        )
        return OddpoolResponse.model_validate(payload)


@pytest.mark.asyncio
async def test_discovery_worker_records_import_metrics() -> None:
    service = DiscoveryService(InMemoryCandidateStore())
    worker = DiscoveryWorker(FixtureClient(), service)

    result = await worker.run_once()

    assert result.imported == 1
    assert worker.polls == 1
    assert worker.imported == 1
