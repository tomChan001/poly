# Polymarket Condition-ID Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Resolve Oddpool Polymarket legs by their native condition ID so valid markets are not rejected when an event slug is not also a market slug.

**Architecture:** Keep native-market resolution inside NativePairMetadataResolver. Select the Gamma query parameter from the strongest identifier available: condition_ids when Oddpool supplies a source condition ID, otherwise the existing slug lookup and event fallback. Preserve all existing unique-condition and token-ID validation.

**Tech Stack:** Python 3.12, httpx, pytest, pytest-asyncio, Polymarket Gamma API

---

## File Structure

- Modify backend/app/adapters/pair_metadata.py: choose the Gamma market query from the Oddpool leg's native condition ID or legacy slug.
- Modify backend/tests/unit/adapters/test_pair_metadata_resolver.py: reproduce the event-slug/market-slug mismatch and assert the condition-ID request.

### Task 1: Query Gamma by native condition ID

**Files:**
- Modify: backend/tests/unit/adapters/test_pair_metadata_resolver.py
- Modify: backend/app/adapters/pair_metadata.py:30-61

- [ ] **Step 1: Write the failing regression test**

Add this test near the other resolver request-selection tests:

~~~python
@pytest.mark.asyncio
async def test_resolver_uses_condition_id_when_event_slug_is_not_a_market_slug() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "kalshi.test":
            return httpx.Response(
                200,
                json={
                    "market": {
                        "ticker": "K-OUTLAWS",
                        "title": "Will Denver Outlaws win?",
                        "status": "open",
                        "rules_primary": "Kalshi native rule",
                        "tick_size": "0.01",
                        "minimum_order_size": "1",
                    }
                },
            )
        if request.url.params.get("condition_ids") == "0xcondition":
            return httpx.Response(
                200,
                json=[
                    {
                        "question": "Will Denver Outlaws win?",
                        "description": "Polymarket native rule",
                        "conditionId": "0xcondition",
                        "outcomes": '["Yes", "No"]',
                        "clobTokenIds": '["token-yes", "token-no"]',
                        "orderMinSize": "1",
                        "orderPriceMinTickSize": "0.01",
                    }
                ],
            )
        return httpx.Response(200, json=[])

    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "oddpool:pll-2026:denver_outlaws",
            "title": "Will Denver Outlaws win?",
            "outcome": "Denver Outlaws",
            "updated_at": "2026-09-04T00:00:00Z",
            "gross_spread": "0.08",
            "estimated_fees": "0.03",
            "legs": [
                {
                    "venue": "kalshi",
                    "outcome": "no",
                    "market_ref": "K-OUTLAWS",
                    "display_price": "0.69",
                },
                {
                    "venue": "polymarket",
                    "outcome": "yes",
                    "market_ref": "premier-league-lacrosse-2026-champion",
                    "display_price": "0.32",
                    "source_condition_id": "0xcondition",
                    "source_token_id": "token-yes",
                },
            ],
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        pair = await NativePairMetadataResolver(
            kalshi_base_url="https://kalshi.test",
            polymarket_gamma_url="https://gamma.test",
            http_client=http,
        ).resolve(opportunity)

    assert requested == [
        "https://kalshi.test/trade-api/v2/markets/K-OUTLAWS",
        "https://gamma.test/markets?condition_ids=0xcondition",
    ]
    assert pair.polymarket_market_id == "token-yes"
~~~

- [ ] **Step 2: Run the test and verify RED**

Run:

~~~powershell
uv run pytest backend/tests/unit/adapters/test_pair_metadata_resolver.py::test_resolver_uses_condition_id_when_event_slug_is_not_a_market_slug -v
~~~

Expected: FAIL because the resolver requests markets?slug=premier-league-lacrosse-2026-champion, receives no market, and raises Polymarket condition ID must resolve to one market.

- [ ] **Step 3: Implement the minimal query selection**

Replace the unconditional Polymarket slug request with:

~~~python
        polymarket_query = (
            {"condition_ids": polymarket_leg.source_condition_id}
            if polymarket_leg.source_condition_id is not None
            else {"slug": slug}
        )
        polymarket_response = await self._http.get(
            f"{self._polymarket_gamma_url}/markets",
            params=polymarket_query,
        )
~~~

Restrict the event fallback to legacy slug resolution:

~~~python
        if not polymarket_candidates and polymarket_leg.source_condition_id is None:
            event_response = await self._http.get(
                f"{self._polymarket_gamma_url}/events",
                params={"slug": slug},
            )
            event_response.raise_for_status()
            polymarket_candidates = _event_markets(_gamma_payload(event_response))
~~~

- [ ] **Step 4: Run the focused resolver suite and verify GREEN**

Run:

~~~powershell
uv run pytest backend/tests/unit/adapters/test_pair_metadata_resolver.py -v
~~~

Expected: all tests PASS, including the new condition-ID request test, legacy slug lookup, event fallback, condition mismatch, and token mismatch tests.

- [ ] **Step 5: Run backend regression tests**

Run:

~~~powershell
uv run pytest backend/tests/unit backend/tests/integration/services/test_pair_discovery.py -q
~~~

Expected: all selected tests PASS with no new warnings or errors.

- [ ] **Step 6: Commit the tested fix**

~~~powershell
git add -- backend/app/adapters/pair_metadata.py backend/tests/unit/adapters/test_pair_metadata_resolver.py
git commit -m "fix: resolve polymarket markets by condition id"
~~~

### Task 2: Verify the reported live candidate

**Files:**
- No file changes.

- [ ] **Step 1: Resolve one fresh Oddpool discovery cycle**

Run the configured discovery service once without printing credentials:

~~~powershell
@'
import asyncio
import json
from backend.app.container import ApplicationContainer

async def main():
    container = ApplicationContainer.runtime()
    try:
        result = await container.pair_discovery.run_once()
        target = "oddpool:premier-league-lacrosse-2026-champion:denver_outlaws"
        print(json.dumps({
            "target_error": next((error for error in result.errors if error.startswith(target)), None),
            "imported": result.imported,
            "updated": result.updated,
            "duplicates": result.duplicates,
        }))
    finally:
        await container.close()

asyncio.run(main())
'@ | uv run python -
~~~

Expected: target_error is null. Other unrelated candidates may still be reported independently.

- [ ] **Step 2: Restart the backend and verify runtime status**

Restart the existing backend process with the project's normal command:

~~~powershell
uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8010
~~~

After one poll cycle, request:

~~~powershell
curl.exe --noproxy "*" http://127.0.0.1:8010/api/runtime/status
~~~

Expected: last_error no longer contains the Denver Outlaws Polymarket condition ID must resolve to one market error. If another independent provider error appears, report it separately rather than attributing it to this fix.

