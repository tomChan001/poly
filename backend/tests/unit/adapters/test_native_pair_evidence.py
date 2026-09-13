from dataclasses import asdict, replace
from datetime import UTC, datetime

import httpx
import pytest

from backend.app.adapters.oddpool.schema import OddpoolOpportunity
from backend.app.adapters.pair_metadata import (
    NativePairMetadataResolver,
    _polymarket_token,
)
from backend.app.db.executable_pairs import _record, _snapshot
from backend.app.domain.enums import MappingStatus
from backend.app.services.executable_pairs import (
    ExecutablePair,
    ExecutablePairService,
    InMemoryExecutablePairRepository,
    build_pair_fingerprints,
)


async def resolve_native(
    kalshi_changes=None,
    polymarket_changes=None,
    *,
    kalshi_url=None,
    polymarket_url=None,
):
    kalshi = {
        "ticker": "K-EVENT-YES",
        "event_ticker": "K-EVENT",
        "title": "Event?",
        "status": "open",
        "rules_primary": "Native primary rule",
        "tick_size": "0.01",
    } | (kalshi_changes or {})
    polymarket = {
        "slug": "market-slug",
        "question": "Event?",
        "description": "Native description",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "orderMinSize": "1",
        "orderPriceMinTickSize": "0.01",
    } | (polymarket_changes or {})
    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "native-evidence",
            "title": "Event?",
            "outcome": "complementary",
            "updated_at": "2026-09-01T00:00:00Z",
            "gross_spread": "0.1",
            "estimated_fees": "0.01",
            "legs": [
                {
                    "venue": "kalshi",
                    "outcome": "no",
                    "market_ref": "K-EVENT-YES",
                    "market_url": kalshi_url,
                    "display_price": "0.4",
                },
                {
                    "venue": "polymarket",
                    "outcome": "yes",
                    "market_ref": "source-slug",
                    "market_url": polymarket_url,
                    "display_price": "0.4",
                },
            ],
        }
    )

    def handler(request):
        return httpx.Response(
            200,
            json={"market": kalshi}
            if request.url.host == "kalshi.test"
            else [polymarket],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        return await NativePairMetadataResolver(
            kalshi_base_url="https://kalshi.test",
            polymarket_gamma_url="https://gamma.test",
            http_client=http,
        ).resolve(opportunity)


@pytest.mark.asyncio
async def test_native_rules_preserved_in_full_with_resolution_source():
    primary = "Primary clause\n" * 500
    secondary = "Secondary clause\n" * 500
    description = "Full Gamma rules\n" * 500
    pair = await resolve_native(
        {"rules_primary": primary, "rules_secondary": secondary},
        {
            "description": description,
            "resolutionSource": "https://results.example/official",
        },
    )
    assert pair.kalshi_rule_text == primary + "\n\n" + secondary
    assert pair.polymarket_rule_text == description
    assert pair.polymarket_resolution_source == "https://results.example/official"


@pytest.mark.asyncio
async def test_expected_settlement_uses_forecast_plus_timer_never_close_time():
    pair = await resolve_native(
        {
            "close_time": "2026-09-15T00:00:00Z",
            "close_date": "2026-09-15T00:00:00Z",
            "expected_expiration_time": "2026-09-16T00:00:00Z",
            "latest_expiration_time": "2026-09-20T00:00:00Z",
            "expiration_time": "2026-09-19T00:00:00Z",
            "settlement_timer_seconds": 3600,
        },
        {"endDate": "2026-09-17T00:00:00Z"},
    )
    assert pair.kalshi_expected_settlement_at == datetime(2026, 9, 16, 1, tzinfo=UTC)
    assert pair.polymarket_expected_settlement_at == datetime(2026, 9, 17, tzinfo=UTC)
    assert pair.worst_case_settlement_at == datetime(2026, 9, 20, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_close_date_only_is_not_a_settlement_estimate():
    pair = await resolve_native({"close_date": "2026-09-15T00:00:00Z"})
    assert pair.kalshi_expected_settlement_at is None


@pytest.mark.asyncio
async def test_market_links_are_separate_from_rule_source_and_official():
    pair = await resolve_native(
        {"rules_url": "https://kalshi.com/rules/contract.pdf"},
        {"events": [{"slug": "native-event"}], "resolutionSource": "Official results"},
        kalshi_url="https://kalshi.com/markets/k-series/topic/k-event",
        polymarket_url="https://polymarket.com.evil.example/event/fake",
    )
    assert pair.kalshi_rule_url == "https://kalshi.com/rules/contract.pdf"
    assert pair.kalshi_market_url == "https://kalshi.com/markets/k-series/topic/k-event"
    assert (
        pair.polymarket_market_url
        == "https://polymarket.com/event/native-event/market-slug"
    )
    assert pair.polymarket_rule_url == pair.polymarket_market_url


@pytest.mark.asyncio
async def test_unknown_kalshi_event_link_is_not_fabricated_from_contract_ticker():
    pair = await resolve_native()
    assert pair.kalshi_market_url == ""


@pytest.mark.asyncio
async def test_native_kalshi_market_link_is_preferred_when_available():
    pair = await resolve_native(
        {"rules_url": "https://kalshi.com/markets/series/title/event"},
        kalshi_url="https://kalshi.com/markets/old",
    )
    assert pair.kalshi_market_url == "https://kalshi.com/markets/series/title/event"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://kalshi.com.evil.test/markets/fake",
        "https://user@kalshi.com/markets/fake",
        "https://kalshi.com:123/markets/fake",
    ],
)
async def test_unofficial_kalshi_market_urls_are_not_exposed(url):
    pair = await resolve_native(kalshi_url=url)
    assert pair.kalshi_market_url == ""


@pytest.mark.asyncio
async def test_latest_expiration_is_fallback_and_actual_settlement_is_not_delayed():
    pair = await resolve_native(
        {
            "latest_expiration_time": "2026-09-19T00:00:00Z",
            "settlement_timer_seconds": 1800,
        }
    )
    assert pair.kalshi_expected_settlement_at == datetime(
        2026, 9, 19, 0, 30, tzinfo=UTC
    )
    settled = await resolve_native(
        {"settlement_ts": "2026-09-19T00:00:00Z", "settlement_timer_seconds": 1800}
    )
    assert settled.kalshi_expected_settlement_at == datetime(2026, 9, 19, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("timer", [-1, True, "3600"])
async def test_invalid_native_settlement_timer_is_rejected(timer):
    with pytest.raises(ValueError, match="settlement_timer_seconds"):
        await resolve_native({"settlement_timer_seconds": timer})


def test_yes_no_token_must_agree_with_requested_outcome():
    with pytest.raises(ValueError, match="outcome"):
        _polymarket_token(
            {"outcomes": ["Yes", "No"], "clobTokenIds": ["yes-token", "no-token"]},
            "yes",
            "no-token",
        )


@pytest.mark.asyncio
async def test_evidence_fields_roundtrip_and_links_are_nonmaterial():
    pair = await resolve_native(polymarket_changes={"resolutionSource": "Source A"})
    stored = ExecutablePair(id="native-evidence", **asdict(pair))
    assert _record(_snapshot(stored)) == stored
    before = build_pair_fingerprints(pair)
    links_changed = replace(
        pair,
        kalshi_market_url="https://kalshi.com/markets/official",
        polymarket_market_url="https://polymarket.com/event/official",
    )
    after = build_pair_fingerprints(links_changed)
    assert before[0] != after[0]
    assert before[1] == after[1]
    assert (
        build_pair_fingerprints(replace(pair, polymarket_resolution_source="Source B"))[
            1
        ]
        != before[1]
    )
    legacy = _snapshot(stored)
    for field in (
        "kalshi_market_url",
        "polymarket_market_url",
        "polymarket_resolution_source",
    ):
        legacy.pop(field)
        assert getattr(_record(legacy), field) == ""


@pytest.mark.asyncio
async def test_unknown_worst_settlement_remains_unknown_when_saved():
    pair = await resolve_native(polymarket_changes={"endDate": "2026-09-17T00:00:00Z"})
    service = ExecutablePairService(InMemoryExecutablePairRepository())
    saved = await service.create(pair)
    assert saved.worst_case_settlement_at is None


@pytest.mark.asyncio
async def test_native_resolution_source_change_invalidates_review():
    pair = await resolve_native(polymarket_changes={"resolutionSource": "Source A"})
    service = ExecutablePairService(InMemoryExecutablePairRepository())
    now = datetime(2026, 9, 14, tzinfo=UTC)
    await service.upsert_discovered(
        pair, source_candidate_id="source", source_updated_at=now
    )
    [saved] = await service.list()
    await service.review(
        saved.id,
        status=MappingStatus.CONDITIONAL,
        checklist={},
        truth_table=[],
        notes="reviewed",
        reviewer="reviewer",
    )
    await service.upsert_discovered(
        replace(pair, polymarket_resolution_source="Source B"),
        source_candidate_id="source",
        source_updated_at=now,
    )
    [changed] = await service.list()
    assert changed.status is MappingStatus.PENDING_REVIEW
