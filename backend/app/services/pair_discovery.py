from dataclasses import dataclass
from typing import Protocol

import httpx

from backend.app.adapters.oddpool.client import OddpoolClient
from backend.app.adapters.oddpool.schema import OddpoolOpportunity, OddpoolResponse
from backend.app.adapters.pair_metadata import NativePairMetadataResolver
from backend.app.services.executable_pairs import (
    ExecutablePairInput,
    ExecutablePairService,
)
from backend.app.services.integration_config import IntegrationConfigService


class OddpoolOpportunitySource(Protocol):
    async def fetch_opportunities(self) -> OddpoolResponse: ...


class PairMetadataResolver(Protocol):
    async def resolve(self, opportunity: OddpoolOpportunity) -> ExecutablePairInput: ...


@dataclass(frozen=True, slots=True)
class PairDiscoveryResult:
    imported: int
    updated: int
    duplicates: int
    failed: int = 0
    errors: tuple[str, ...] = ()


class OddpoolPairDiscoveryService:
    """Turns Oddpool candidates into review-only executable-pair drafts."""

    def __init__(
        self,
        source: OddpoolOpportunitySource,
        resolver: PairMetadataResolver,
        pairs: ExecutablePairService,
    ) -> None:
        self._source = source
        self._resolver = resolver
        self._pairs = pairs

    async def run_once(self) -> PairDiscoveryResult:
        payload = await self._source.fetch_opportunities()
        imported = 0
        updated = 0
        duplicates = 0
        errors: list[str] = []
        for opportunity in payload.opportunities:
            try:
                value = await self._resolver.resolve(opportunity)
                change = await self._pairs.upsert_discovered(
                    value,
                    source_candidate_id=opportunity.id,
                    source_updated_at=opportunity.updated_at,
                )
            except Exception as exc:  # noqa: BLE001 - one malformed candidate is isolated
                errors.append(f"{opportunity.id}: {exc}")
                continue
            imported += int(change == "imported")
            updated += int(change == "updated")
            duplicates += int(change == "duplicate")
        return PairDiscoveryResult(imported, updated, duplicates, len(errors), tuple(errors))


class ConfiguredOddpoolPairDiscoveryService:
    """Builds one discovery cycle from the current web-saved configuration."""

    def __init__(
        self,
        integrations: IntegrationConfigService,
        pairs: ExecutablePairService,
        http_client: httpx.AsyncClient,
        *,
        polymarket_gamma_url: str,
    ) -> None:
        self._integrations = integrations
        self._pairs = pairs
        self._http = http_client
        self._polymarket_gamma_url = polymarket_gamma_url

    async def run_once(self) -> PairDiscoveryResult:
        bundle = await self._integrations.runtime_bundle()
        source = OddpoolClient(
            bundle.oddpool.record.base_url,
            bundle.oddpool.credentials["api_token"],
            self._http,
        )
        resolver = NativePairMetadataResolver(
            kalshi_base_url=bundle.kalshi.record.base_url,
            polymarket_gamma_url=self._polymarket_gamma_url,
            http_client=self._http,
        )
        return await OddpoolPairDiscoveryService(source, resolver, self._pairs).run_once()
