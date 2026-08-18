from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid4

from backend.app.domain.enums import MappingStatus


@dataclass(frozen=True, slots=True)
class RuleVersionRecord:
    id: UUID
    market_id: str
    content_hash: str
    text: str
    source_url: str


@dataclass(slots=True)
class PairMappingRecord:
    id: UUID
    kalshi_market_id: str
    polymarket_market_id: str
    kalshi_rule_version_id: UUID
    polymarket_rule_version_id: UUID
    status: MappingStatus


class InMemoryRuleStore:
    def __init__(self) -> None:
        self.rule_versions: dict[UUID, RuleVersionRecord] = {}
        self.rule_index: dict[tuple[str, str], UUID] = {}
        self.latest_by_market: dict[str, UUID] = {}
        self.mappings: dict[UUID, PairMappingRecord] = {}
        self.reviews: list[object] = []

    def create_mapping(
        self,
        kalshi_market_id: str,
        polymarket_market_id: str,
        kalshi_rule_version_id: UUID,
        polymarket_rule_version_id: UUID,
        status: MappingStatus,
    ) -> PairMappingRecord:
        mapping = PairMappingRecord(
            id=uuid4(),
            kalshi_market_id=kalshi_market_id,
            polymarket_market_id=polymarket_market_id,
            kalshi_rule_version_id=kalshi_rule_version_id,
            polymarket_rule_version_id=polymarket_rule_version_id,
            status=status,
        )
        self.mappings[mapping.id] = mapping
        return mapping


class RuleService:
    def __init__(self, store: InMemoryRuleStore) -> None:
        self._store = store

    def record_version(self, market_id: str, text: str, source_url: str) -> RuleVersionRecord:
        content_hash = sha256(text.encode("utf-8")).hexdigest()
        existing_id = self._store.rule_index.get((market_id, content_hash))
        if existing_id is not None:
            return self._store.rule_versions[existing_id]

        previous_id = self._store.latest_by_market.get(market_id)
        version = RuleVersionRecord(uuid4(), market_id, content_hash, text, source_url)
        self._store.rule_versions[version.id] = version
        self._store.rule_index[(market_id, content_hash)] = version.id
        self._store.latest_by_market[market_id] = version.id

        if previous_id is not None:
            self._invalidate_mappings(market_id, previous_id)
        return version

    def _invalidate_mappings(self, market_id: str, previous_id: UUID) -> None:
        for mapping in self._store.mappings.values():
            references_changed_rule = (
                mapping.kalshi_market_id == market_id
                and mapping.kalshi_rule_version_id == previous_id
            ) or (
                mapping.polymarket_market_id == market_id
                and mapping.polymarket_rule_version_id == previous_id
            )
            if references_changed_rule and mapping.status is MappingStatus.EXACT:
                mapping.status = MappingStatus.STALE

