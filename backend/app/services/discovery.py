from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from backend.app.adapters.oddpool.schema import OddpoolOpportunity


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    source_candidate_id: str
    source_updated_at: datetime
    title: str
    outcome: str
    resolves_at: datetime | None
    source_urls: dict[str, str]
    source_prices: dict[str, str]
    source_link_invalid: bool


@dataclass(frozen=True, slots=True)
class IngestResult:
    imported: int
    duplicates: int


class InMemoryCandidateStore:
    def __init__(self) -> None:
        self.candidates: dict[tuple[str, datetime], DiscoveryCandidate] = {}

    async def add_if_absent(self, candidate: DiscoveryCandidate) -> bool:
        key = (candidate.source_candidate_id, candidate.source_updated_at)
        if key in self.candidates:
            return False
        self.candidates[key] = candidate
        return True


def _is_valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class DiscoveryService:
    def __init__(self, store: InMemoryCandidateStore) -> None:
        self._store = store

    async def ingest(self, opportunities: list[OddpoolOpportunity]) -> IngestResult:
        imported = 0
        for opportunity in opportunities:
            urls = {leg.venue.value: leg.market_url for leg in opportunity.legs}
            prices = {leg.venue.value: leg.display_price for leg in opportunity.legs}
            candidate = DiscoveryCandidate(
                source_candidate_id=opportunity.id,
                source_updated_at=opportunity.updated_at,
                title=opportunity.title,
                outcome=opportunity.outcome,
                resolves_at=opportunity.resolves_at,
                source_urls=urls,
                source_prices=prices,
                source_link_invalid=not all(_is_valid_http_url(url) for url in urls.values()),
            )
            imported += int(await self._store.add_if_absent(candidate))

        return IngestResult(imported=imported, duplicates=len(opportunities) - imported)

