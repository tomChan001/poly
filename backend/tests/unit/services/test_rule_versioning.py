from backend.app.domain.enums import MappingStatus
from backend.app.services.rules import InMemoryRuleStore, RuleService


def test_changed_rule_hash_invalidates_exact_mapping() -> None:
    store = InMemoryRuleStore()
    service = RuleService(store)
    first = service.record_version("kalshi-market", "original rules", "https://kalshi/rule")
    other = service.record_version("poly-market", "matching rules", "https://poly/rule")
    mapping = store.create_mapping(
        "kalshi-market",
        "poly-market",
        first.id,
        other.id,
        MappingStatus.EXACT,
    )

    service.record_version("kalshi-market", "changed rules", "https://kalshi/rule")

    assert store.mappings[mapping.id].status is MappingStatus.STALE


def test_recording_identical_rule_is_idempotent() -> None:
    store = InMemoryRuleStore()
    service = RuleService(store)

    first = service.record_version("market-1", "same rules", "https://example/rule")
    second = service.record_version("market-1", "same rules", "https://example/rule")

    assert first.id == second.id

