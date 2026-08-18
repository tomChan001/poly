from typing import Protocol

from prometheus_client import Counter

from backend.app.adapters.oddpool.schema import OddpoolResponse
from backend.app.services.discovery import DiscoveryService, IngestResult

ODDPOOL_POLLS = Counter("oddpool_poll_total", "Oddpool discovery polls")
ODDPOOL_IMPORTED = Counter(
    "oddpool_candidates_imported_total",
    "Oddpool candidates imported after deduplication",
)


class DiscoveryClient(Protocol):
    async def fetch_opportunities(self) -> OddpoolResponse: ...


class DiscoveryWorker:
    def __init__(self, client: DiscoveryClient, service: DiscoveryService) -> None:
        self._client = client
        self._service = service
        self.polls = 0
        self.imported = 0

    async def run_once(self) -> IngestResult:
        response = await self._client.fetch_opportunities()
        result = await self._service.ingest(response.opportunities)
        self.polls += 1
        self.imported += result.imported
        ODDPOOL_POLLS.inc()
        ODDPOOL_IMPORTED.inc(result.imported)
        return result
