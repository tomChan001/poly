# Fixed Oddpool API Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the editable, undocumented Oddpool integration with a backend-enforced `https://api.oddpool.com` connection that uses the official arbitrage endpoint, authentication header, and response schema while preserving native-venue verification.

**Architecture:** Keep Oddpool behind its adapter boundary. The configuration service enforces one canonical host, the Oddpool adapter converts the vendor response into the existing discovery model, and the native metadata resolver consumes explicit market references and cross-checks Oddpool identifiers against Kalshi and Polymarket before creating review drafts. The frontend only displays the effective Oddpool endpoint; Kalshi and Polymarket remain configurable.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, httpx, SQLAlchemy/Alembic, React 19, TypeScript 6, Vitest, pytest, ruff, mypy.

---

## File map

- `backend/app/services/integration_config.py`: canonical Oddpool endpoint policy and update validation.
- `migrations/versions/0006_fixed_oddpool_endpoint.py`: normalize previously stored Oddpool endpoints.
- `backend/app/adapters/oddpool/schema.py`: official Oddpool DTOs and conversion to internal discovery models.
- `backend/app/adapters/oddpool/client.py`: official request path, API-key header, bounded 429 retry, and row-isolated parsing.
- `backend/app/adapters/integration_probe.py`: read-only connection test against the official endpoint.
- `backend/app/adapters/pair_metadata.py`: explicit market-reference consumption and native identifier cross-checks.
- `backend/app/services/pair_discovery.py`: construct the fixed-host client and propagate source-row errors.
- `frontend/src/pages/IntegrationSettingsPage.tsx`: fixed Oddpool endpoint display and canonical save payload.
- `README.md`: operator-facing Oddpool endpoint and discovery-only documentation.
- Backend/frontend test and fixture files listed in each task: regression coverage for every boundary.

### Task 1: Enforce the canonical Oddpool endpoint

**Files:**
- Modify: `backend/app/services/integration_config.py`
- Create: `migrations/versions/0006_fixed_oddpool_endpoint.py`
- Modify: `backend/tests/integration/api/test_integrations.py`

- [ ] **Step 1: Write API tests for the fixed endpoint**

Change the shared Oddpool payload and add the rejection test in `backend/tests/integration/api/test_integrations.py`:

```python
from backend.app.services.integration_config import ODDPOOL_BASE_URL


def integration_payload(token: str = "oddpool-secret-token") -> dict[str, object]:
    return {
        "enabled": True,
        "environment": "production",
        "base_url": ODDPOOL_BASE_URL,
        "configuration": {},
        "secrets": {"api_token": token},
    }


@pytest.mark.asyncio
async def test_oddpool_rejects_noncanonical_endpoint() -> None:
    payload = integration_payload()
    payload["base_url"] = "https://credential-exfiltration.example"
    transport = httpx.ASGITransport(app=app_for(Role.OPERATOR))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/api/integrations/oddpool", json=payload)
        listed = await client.get("/api/integrations")

    assert response.status_code == 422
    assert response.json() == {
        "detail": "oddpool base URL is fixed at https://api.oddpool.com"
    }
    assert listed.json() == []
```

- [ ] **Step 2: Run the endpoint test and confirm it fails**

Run:

```powershell
uv run pytest backend/tests/integration/api/test_integrations.py::test_oddpool_rejects_noncanonical_endpoint -q
```

Expected: FAIL because the service currently accepts any HTTPS URL.

- [ ] **Step 3: Add the canonical endpoint policy**

Add the constant and normalization helper to `backend/app/services/integration_config.py`:

```python
ODDPOOL_BASE_URL = "https://api.oddpool.com"


def canonical_base_url(provider: IntegrationProvider, base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if provider is IntegrationProvider.ODDPOOL:
        if normalized != ODDPOOL_BASE_URL:
            raise ValueError(
                f"oddpool base URL is fixed at {ODDPOOL_BASE_URL}"
            )
        return ODDPOOL_BASE_URL
    return normalized
```

At the start of `IntegrationConfigService.update()`, before validating or saving secrets, normalize the value:

```python
normalized_base_url = canonical_base_url(provider, base_url)
```

Use `normalized_base_url` when constructing `IntegrationConfigRecord`. Validating before secret writes ensures a rejected host cannot modify credential state.

- [ ] **Step 4: Add the data migration**

Create `migrations/versions/0006_fixed_oddpool_endpoint.py`:

```python
"""Normalize the fixed Oddpool production endpoint.

Revision ID: 0006_fixed_oddpool_endpoint
Revises: 0005_prelive_safety
Create Date: 2026-09-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_fixed_oddpool_endpoint"
down_revision: str | None = "0005_prelive_safety"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE integration_config "
        "SET base_url = 'https://api.oddpool.com', version = version + 1 "
        "WHERE provider = 'oddpool' "
        "AND base_url <> 'https://api.oddpool.com'"
    )


def downgrade() -> None:
    # The previous operator-supplied URL is intentionally not recoverable.
    pass
```

- [ ] **Step 5: Run focused tests and migration checks**

Run:

```powershell
uv run pytest backend/tests/integration/api/test_integrations.py -q
$env:DATABASE_URL='postgresql+asyncpg://poly:poly@localhost:55432/poly'
uv run alembic upgrade head
uv run alembic current
```

Expected: integration API tests pass and Alembic reports `0006_fixed_oddpool_endpoint (head)`.

- [ ] **Step 6: Commit the endpoint boundary**

```powershell
git add backend/app/services/integration_config.py backend/tests/integration/api/test_integrations.py migrations/versions/0006_fixed_oddpool_endpoint.py
git commit -m "fix: enforce official Oddpool endpoint"
```

### Task 2: Implement the official Oddpool request and response contract

**Files:**
- Modify: `backend/app/adapters/oddpool/schema.py`
- Modify: `backend/app/adapters/oddpool/client.py`
- Modify: `backend/app/adapters/integration_probe.py`
- Create: `backend/tests/fixtures/oddpool/arbitrage_current.json`
- Modify: `backend/tests/unit/adapters/test_oddpool.py`
- Modify: `backend/tests/unit/adapters/test_authenticated_transports.py`

- [ ] **Step 1: Add an official response fixture**

Create `backend/tests/fixtures/oddpool/arbitrage_current.json` using the official field names and numeric units:

```json
[
  {
    "event_id": 42,
    "event_title": "Fed rate decision March",
    "kalshi_event_ticker": "KXFEDDECISION-26MAR",
    "polymarket_event_slug": "fed-rate-march",
    "market_type": "binary",
    "outcome_key": "c25",
    "label": "25bps cut",
    "timestamp": "2026-03-11T14:30:00",
    "resolution_time": "2026-03-19T18:00:00",
    "kalshi": {
      "market_ticker": "KXFEDDECISION-26MAR-C25",
      "yes_ask": 0.32,
      "no_ask": 0.69
    },
    "polymarket": {
      "condition_id": "0xde04",
      "yes_token_id": "token-yes",
      "no_token_id": "token-no",
      "yes_ask": 0.39,
      "no_ask": 0.60
    },
    "buy_yes_market": "kalshi",
    "buy_no_market": "polymarket",
    "gross_cents": 8.0,
    "fee_cents": 3,
    "net_cents": 5.0
  }
]
```

- [ ] **Step 2: Replace the old client-contract test with official assertions**

In `backend/tests/unit/adapters/test_oddpool.py`, load the new fixture and assert the official endpoint, header, conversion, UTC normalization, stable ID, and absence of bearer authentication:

```python
def load_official_fixture() -> list[dict[str, object]]:
    return json.loads(
        Path("backend/tests/fixtures/oddpool/arbitrage_current.json").read_text()
    )


@pytest.mark.asyncio
async def test_oddpool_client_uses_official_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://api.oddpool.com/arbitrage/current"
        )
        assert request.headers["x-api-key"] == "test-token"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=load_official_fixture())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http_client:
        response = await OddpoolClient("test-token", http_client).fetch_opportunities()

    opportunity = response.opportunities[0]
    assert opportunity.id == "oddpool:42:c25"
    assert opportunity.updated_at == datetime(2026, 3, 11, 14, 30, tzinfo=UTC)
    assert opportunity.resolves_at == datetime(2026, 3, 19, 18, 0, tzinfo=UTC)
    assert opportunity.gross_spread == "0.08"
    assert opportunity.estimated_fees == "0.03"
    assert [(leg.venue.value, leg.outcome, leg.market_ref) for leg in opportunity.legs] == [
        ("kalshi", "yes", "KXFEDDECISION-26MAR-C25"),
        ("polymarket", "no", "fed-rate-march"),
    ]
```

Add tests that append one malformed row and one Opinion-only row to the official fixture. Assert the valid opportunity remains, the malformed row produces a generic `row 1: invalid Oddpool arbitrage row` error, and the unsupported venue pair is skipped.

Add a 429 test by injecting an async sleeper that records delays. Return 429 twice and then 200; assert delays are `[1, 2]` and the third request succeeds.

Use these complete tests:

```python
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
```

- [ ] **Step 3: Run the official-contract tests and confirm they fail**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_oddpool.py -q
```

Expected: FAIL because `OddpoolClient` still requires a base URL, sends bearer authentication, calls `/api/opportunities`, and expects a wrapped object.

- [ ] **Step 4: Define source DTOs and explicit internal references**

In `backend/app/adapters/oddpool/schema.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OddpoolLeg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    venue: Venue
    outcome: Literal["yes", "no"]
    market_ref: str
    market_url: str | None = None
    display_price: str
    source_condition_id: str | None = None
    source_token_id: str | None = None


class OddpoolOfficialVenue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market_ticker: str | None = None
    condition_id: str | None = None
    yes_token_id: str | None = None
    no_token_id: str | None = None
    yes_ask: Decimal | None = None
    no_ask: Decimal | None = None


class OddpoolArbitrageRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: int | str
    event_title: str
    polymarket_event_slug: str
    outcome_key: str
    label: str
    timestamp: datetime
    resolution_time: datetime | None = None
    kalshi: OddpoolOfficialVenue
    polymarket: OddpoolOfficialVenue
    buy_yes_market: str
    buy_no_market: str
    gross_cents: Decimal
    fee_cents: Decimal

    @field_validator("timestamp", "resolution_time")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def to_opportunity(self) -> "OddpoolOpportunity | None":
        sides = {
            self.buy_yes_market.casefold(): "yes",
            self.buy_no_market.casefold(): "no",
        }
        if set(sides) != {"kalshi", "polymarket"}:
            return None
        if not self.kalshi.market_ticker:
            raise ValueError("Kalshi market ticker is missing")
        if not self.polymarket.condition_id:
            raise ValueError("Polymarket condition ID is missing")

        kalshi_side = sides["kalshi"]
        polymarket_side = sides["polymarket"]
        kalshi_price = getattr(self.kalshi, f"{kalshi_side}_ask")
        polymarket_price = getattr(self.polymarket, f"{polymarket_side}_ask")
        polymarket_token = getattr(
            self.polymarket,
            f"{polymarket_side}_token_id",
        )
        if kalshi_price is None or polymarket_price is None or not polymarket_token:
            raise ValueError("selected Oddpool leg is incomplete")

        return OddpoolOpportunity(
            id=f"oddpool:{self.event_id}:{self.outcome_key}",
            title=self.event_title,
            outcome=self.label,
            updated_at=self.timestamp,
            resolves_at=self.resolution_time,
            gross_spread=str(self.gross_cents / Decimal(100)),
            estimated_fees=str(self.fee_cents / Decimal(100)),
            legs=[
                OddpoolLeg(
                    venue=Venue.KALSHI,
                    outcome=kalshi_side,
                    market_ref=self.kalshi.market_ticker,
                    market_url=f"https://kalshi.com/markets/{self.kalshi.market_ticker}",
                    display_price=str(kalshi_price),
                ),
                OddpoolLeg(
                    venue=Venue.POLYMARKET,
                    outcome=polymarket_side,
                    market_ref=self.polymarket_event_slug,
                    market_url=f"https://polymarket.com/event/{self.polymarket_event_slug}",
                    display_price=str(polymarket_price),
                    source_condition_id=self.polymarket.condition_id,
                    source_token_id=polymarket_token,
                ),
            ],
        )


class OddpoolResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    opportunities: list[OddpoolOpportunity]
    errors: tuple[str, ...] = ()
```

Keep `OddpoolOpportunity.require_one_leg_per_supported_venue()` so the internal model still rejects any non-Kalshi/Polymarket pair.

- [ ] **Step 5: Implement official fetching, row isolation, and bounded retry**

Change `backend/app/adapters/oddpool/client.py` so the constructor takes only the API key, client, optional sleeper, and attempt limit. Parse numbers through `json.loads(..., parse_float=Decimal)` and validate rows one at a time:

```python
import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx
from pydantic import ValidationError

from backend.app.adapters.oddpool.schema import (
    OddpoolArbitrageRow,
    OddpoolResponse,
)
from backend.app.services.integration_config import ODDPOOL_BASE_URL

Sleeper = Callable[[float], Awaitable[None]]


class OddpoolClient:
    def __init__(
        self,
        api_token: str,
        http_client: httpx.AsyncClient,
        *,
        sleeper: Sleeper = asyncio.sleep,
        max_attempts: int = 6,
    ) -> None:
        self._api_token = api_token
        self._http = http_client
        self._sleeper = sleeper
        self._max_attempts = max_attempts

    async def fetch_opportunities(self) -> OddpoolResponse:
        response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            response = await self._http.get(
                f"{ODDPOOL_BASE_URL}/arbitrage/current",
                headers={"X-API-Key": self._api_token},
            )
            if response.status_code != 429 or attempt == self._max_attempts - 1:
                break
            await self._sleeper(retry_delay_seconds(attempt))
        assert response is not None
        response.raise_for_status()

        payload = json.loads(response.text, parse_float=Decimal)
        if not isinstance(payload, list):
            raise TypeError("Oddpool arbitrage response must be a list")
        opportunities = []
        errors = []
        for index, item in enumerate(payload):
            try:
                opportunity = OddpoolArbitrageRow.model_validate(item).to_opportunity()
            except (ValidationError, ValueError, TypeError):
                errors.append(f"row {index}: invalid Oddpool arbitrage row")
                continue
            if opportunity is not None:
                opportunities.append(opportunity)
        return OddpoolResponse(opportunities=opportunities, errors=tuple(errors))
```

- [ ] **Step 6: Update the read-only connection probe**

In `backend/app/adapters/integration_probe.py`, import `ODDPOOL_BASE_URL` and change `_request()` to:

```python
if record.provider is IntegrationProvider.ODDPOOL:
    return (
        f"{ODDPOOL_BASE_URL}/arbitrage/current",
        {"X-API-Key": secrets["api_token"]},
        "authenticated Oddpool request",
    )
```

Add a test to `backend/tests/unit/adapters/test_authenticated_transports.py` whose mock handler asserts the exact URL/header, asserts `authorization` is absent, returns `[]`, and expects `ODDPOOL_CONNECTION_OK`.

```python
@pytest.mark.asyncio
async def test_oddpool_probe_uses_official_read_only_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://api.oddpool.com/arbitrage/current"
        )
        assert request.headers["x-api-key"] == "oddpool-key"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[])

    record = IntegrationConfigRecord(
        provider=IntegrationProvider.ODDPOOL,
        enabled=True,
        environment=IntegrationEnvironment.PRODUCTION,
        base_url="https://api.oddpool.com",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await HttpIntegrationConnectionProbe(client).test(
            record,
            {"api_token": "oddpool-key"},
        )

    assert result.ok is True
    assert result.code == "ODDPOOL_CONNECTION_OK"
```

- [ ] **Step 7: Run the adapter tests**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_oddpool.py backend/tests/unit/adapters/test_authenticated_transports.py -q
uv run mypy backend/app/adapters/oddpool backend/app/adapters/integration_probe.py
```

Expected: tests pass and mypy reports no issues.

- [ ] **Step 8: Commit the official API adapter**

```powershell
git add backend/app/adapters/oddpool backend/app/adapters/integration_probe.py backend/tests/fixtures/oddpool backend/tests/unit/adapters
git commit -m "fix: consume official Oddpool arbitrage API"
```

### Task 3: Use explicit market references and cross-check native metadata

**Files:**
- Modify: `backend/app/adapters/pair_metadata.py`
- Modify: `backend/app/services/pair_discovery.py`
- Modify: `backend/tests/fixtures/oddpool/opportunities.json`
- Modify: `backend/tests/unit/adapters/test_pair_metadata_resolver.py`
- Modify: `backend/tests/integration/services/test_pair_discovery.py`

- [ ] **Step 1: Update internal fixtures to require explicit references**

For every internal `OddpoolLeg` fixture, add `market_ref`. Use the Kalshi ticker and Polymarket slug already present in the URLs:

```json
{
  "venue": "kalshi",
  "outcome": "no",
  "market_ref": "K-EVENT",
  "market_url": "https://kalshi.com/markets/K-EVENT",
  "display_price": "0.70"
}
```

```json
{
  "venue": "polymarket",
  "outcome": "yes",
  "market_ref": "event-slug",
  "market_url": "https://polymarket.com/event/event-slug",
  "display_price": "0.20",
  "source_condition_id": "0xcondition",
  "source_token_id": "token-yes"
}
```

- [ ] **Step 2: Add native cross-check tests**

In `backend/tests/unit/adapters/test_pair_metadata_resolver.py`, update existing opportunities with `market_ref`, then add this parametrized native-identity test:

```python
@pytest.mark.parametrize(
    ("condition_id", "token_id", "message"),
    [
        ("0xwrong", "token-yes", "condition ID must resolve to one market"),
        ("0xcondition", "wrong-token", "Polymarket token ID mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_resolver_rejects_oddpool_native_id_mismatch(
    condition_id: str,
    token_id: str,
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "kalshi.test":
            return httpx.Response(
                200,
                json={
                    "market": {
                        "ticker": "K-EVENT",
                        "title": "Will the event happen?",
                        "status": "open",
                        "rules_primary": "Kalshi native rule",
                        "tick_size": "0.01",
                        "minimum_order_size": "1",
                    }
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "question": "Will the event happen?",
                    "description": "Polymarket native rule",
                    "conditionId": "0xcondition",
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["token-yes", "token-no"]',
                    "orderMinSize": "1",
                    "orderPriceMinTickSize": "0.01",
                }
            ],
        )

    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "oddpool:42:yes",
            "title": "Will the event happen?",
            "outcome": "yes",
            "updated_at": "2026-09-01T00:00:00Z",
            "gross_spread": "0.08",
            "estimated_fees": "0.03",
            "legs": [
                {
                    "venue": "kalshi",
                    "outcome": "no",
                    "market_ref": "K-EVENT",
                    "display_price": "0.69",
                },
                {
                    "venue": "polymarket",
                    "outcome": "yes",
                    "market_ref": "event-slug",
                    "display_price": "0.32",
                    "source_condition_id": condition_id,
                    "source_token_id": token_id,
                },
            ],
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as http:
        resolver = NativePairMetadataResolver(
            kalshi_base_url="https://kalshi.test",
            polymarket_gamma_url="https://gamma.test",
            http_client=http,
        )
        with pytest.raises(ValueError, match=message):
            await resolver.resolve(opportunity)
```

- [ ] **Step 3: Run the cross-check test and confirm it fails**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_pair_metadata_resolver.py -q
```

Expected: FAIL because the resolver still derives identifiers from `market_url` and does not compare Oddpool evidence with native metadata.

- [ ] **Step 4: Consume `market_ref` and verify condition/token identity**

In `NativePairMetadataResolver.resolve()` replace URL parsing with:

```python
ticker = kalshi_leg.market_ref
slug = polymarket_leg.market_ref
```

After selecting the Polymarket market and deriving the selected native token, add:

```python
native_condition_id = _optional_text(polymarket, "conditionId", "condition_id")
if (
    polymarket_leg.source_condition_id is not None
    and native_condition_id != polymarket_leg.source_condition_id
):
    raise ValueError("Polymarket condition ID mismatch")
if (
    polymarket_leg.source_token_id is not None
    and token_id != polymarket_leg.source_token_id
):
    raise ValueError("Polymarket token ID mismatch")
```

Add the helper:

```python
def _optional_text(payload: dict[str, object], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
```

Update `_select_polymarket_market()` to prefer an expected condition ID before title matching:

```python
def _select_polymarket_market(
    markets: list[dict[str, object]],
    opportunity_title: str,
    expected_condition_id: str | None = None,
) -> dict[str, object]:
    if expected_condition_id is not None:
        matches = [
            market
            for market in markets
            if _optional_text(market, "conditionId", "condition_id")
            == expected_condition_id
        ]
        if len(matches) != 1:
            raise ValueError("Polymarket condition ID must resolve to one market")
        return matches[0]
    if len(markets) == 1:
        return markets[0]
    title = opportunity_title.strip().casefold()
    matches = [
        market
        for market in markets
        if str(market.get("question", "")).strip().casefold() == title
    ]
    if len(matches) != 1:
        raise ValueError("Polymarket reference must resolve to one uniquely matched market")
    return matches[0]
```

Pass `polymarket_leg.source_condition_id` at the call site.

- [ ] **Step 5: Construct the fixed-host client and propagate row errors**

In `ConfiguredOddpoolPairDiscoveryService.run_once()` construct the new client without a stored base URL:

```python
source = OddpoolClient(
    bundle.oddpool.credentials["api_token"],
    self._http,
)
```

In `OddpoolPairDiscoveryService.run_once()`, seed errors from the source response and include them in the failed count:

```python
errors: list[str] = list(payload.errors)
```

Keep candidate-specific resolver errors appended after those source-row errors.

- [ ] **Step 6: Run discovery and metadata tests**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_pair_metadata_resolver.py backend/tests/integration/services/test_pair_discovery.py backend/tests/unit/services/test_discovery_worker.py -q
uv run mypy backend/app/adapters/pair_metadata.py backend/app/services/pair_discovery.py
```

Expected: all tests pass; explicit references are used; condition/token mismatches fail closed; malformed rows do not suppress valid candidates.

- [ ] **Step 7: Commit the native verification boundary**

```powershell
git add backend/app/adapters/pair_metadata.py backend/app/services/pair_discovery.py backend/tests/fixtures/oddpool/opportunities.json backend/tests/unit/adapters/test_pair_metadata_resolver.py backend/tests/integration/services/test_pair_discovery.py backend/tests/unit/services/test_discovery_worker.py
git commit -m "fix: verify Oddpool candidates against native IDs"
```

### Task 4: Replace the editable Oddpool URL field with fixed endpoint text

**Files:**
- Modify: `frontend/src/pages/IntegrationSettingsPage.tsx`
- Modify: `frontend/src/App.test.tsx`

- [ ] **Step 1: Write the failing UI assertions**

Update the integration-menu test in `frontend/src/App.test.tsx`:

```typescript
expect(screen.getByText('https://api.oddpool.com')).toBeInTheDocument()
expect(
  screen.queryByRole('textbox', { name: 'Oddpool API 地址' }),
).not.toBeInTheDocument()
expect(screen.getByLabelText('Oddpool API Token')).toHaveAttribute('type', 'password')
expect(screen.getByRole('textbox', { name: 'Kalshi API 地址' })).toBeEnabled()
expect(screen.getByRole('textbox', { name: 'Polymarket CLOB 地址' })).toBeEnabled()
```

Add a save test that captures the Oddpool PUT body and asserts `base_url` is exactly `https://api.oddpool.com`.

```typescript
test('submits the canonical Oddpool endpoint', async () => {
  let submitted: Record<string, unknown> | null = null
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/integrations/oddpool') && init?.method === 'PUT') {
        submitted = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve({
          ok: true,
          json: async () => ({
            provider: 'oddpool',
            enabled: false,
            environment: 'production',
            base_url: 'https://api.oddpool.com',
            configuration: {},
            version: 1,
            updated_at: '2026-09-01T00:00:00Z',
            updated_by: 'operator-1',
            secret_status: {
              api_token: { configured: true, fingerprint: 'sha256:123456789abc' },
            },
          }),
        })
      }
      if (url.endsWith('/api/integrations')) {
        return Promise.resolve({ ok: true, json: async () => [] })
      }
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      if (url.includes('/health')) {
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '集成' }))
  fireEvent.change(await screen.findByLabelText('Oddpool API Token'), {
    target: { value: 'oddpool-key' },
  })
  const panel = screen.getByRole('heading', { name: 'Oddpool' }).closest('form')
  expect(panel).not.toBeNull()
  fireEvent.click(within(panel!).getByRole('button', { name: '保存' }))

  await waitFor(() => expect(submitted).not.toBeNull())
  expect(submitted).toMatchObject({
    base_url: 'https://api.oddpool.com',
    secrets: { api_token: 'oddpool-key' },
  })
})
```

- [ ] **Step 2: Run the UI test and confirm it fails**

Run:

```powershell
cd frontend
npm run test -- src/App.test.tsx
```

Expected: FAIL because the Oddpool URL is currently an empty editable textbox.

- [ ] **Step 3: Render a provider-specific fixed endpoint**

In `IntegrationSettingsPage.tsx`, add:

```typescript
const ODDPOOL_BASE_URL = 'https://api.oddpool.com'
```

Set Oddpool's `defaultUrl` to that constant. Replace the universal endpoint label block with:

```tsx
{definition.provider === 'oddpool' ? (
  <div className="field-wide integration-fixed-endpoint">
    <span>Oddpool API 地址（固定）</span>
    <code>{ODDPOOL_BASE_URL}</code>
  </div>
) : (
  <label className="field-wide">
    <span>{definition.endpointLabel}</span>
    <input
      aria-label={definition.endpointLabel}
      type="url"
      required
      value={config.baseUrl}
      onChange={(event) => update(definition.provider, {
        baseUrl: event.target.value,
      })}
    />
  </label>
)}
```

When building the save payload, force the canonical value independently of loaded state:

```typescript
base_url: provider === 'oddpool' ? ODDPOOL_BASE_URL : config.baseUrl,
```

This prevents a stale pre-migration value from being resubmitted by an already-open browser tab.

- [ ] **Step 4: Run frontend tests, lint, and build**

Run:

```powershell
cd frontend
npm run test -- src/App.test.tsx
npm run lint
npm run build
```

Expected: App tests pass, oxlint reports no errors, and Vite produces a successful production build.

- [ ] **Step 5: Commit the fixed endpoint UI**

```powershell
git add frontend/src/pages/IntegrationSettingsPage.tsx frontend/src/App.test.tsx
git commit -m "fix: make Oddpool endpoint read only"
```

### Task 5: Document and verify the complete integration

**Files:**
- Modify: `README.md`
- Verify: `docs/superpowers/specs/2026-09-01-fixed-oddpool-api-design.md`

- [ ] **Step 1: Update operator documentation**

Add this Oddpool section after the local integration instructions in `README.md`:

```markdown
### Oddpool API

Oddpool 固定连接官方生产地址 `https://api.oddpool.com`，使用
`X-API-Key` 调用只读的 `/arbitrage/current`。运营员只需填写 API Key；
页面不允许修改服务地址。Oddpool 数据仅用于发现候选，规则、订单簿、费用、
余额和成交仍必须由 Kalshi 与 Polymarket 原生接口复核。

Oddpool 当前产品说明和服务条款对自动化交易用途存在表述差异。真实自动执行前，
运营方必须自行取得 Oddpool 对该用途的确认或授权。
```

- [ ] **Step 2: Run the focused backend suite**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_oddpool.py backend/tests/unit/adapters/test_authenticated_transports.py backend/tests/unit/adapters/test_pair_metadata_resolver.py backend/tests/integration/api/test_integrations.py backend/tests/integration/services/test_pair_discovery.py backend/tests/unit/services/test_discovery_worker.py -q
```

Expected: all focused tests pass.

- [ ] **Step 3: Run the repository-wide backend checks**

Run:

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy backend/app
```

Expected: pytest passes, ruff reports no violations, and mypy reports no errors.

- [ ] **Step 4: Run the repository-wide frontend checks**

Run:

```powershell
cd frontend
npm run test
npm run lint
npm run build
```

Expected: Vitest passes, oxlint reports no violations, TypeScript compilation succeeds, and Vite builds successfully.

- [ ] **Step 5: Perform a local read-only smoke test**

Start the existing local database/backend/frontend stack. Without entering a real API key, verify:

```powershell
Invoke-RestMethod http://127.0.0.1:8010/health
Invoke-RestMethod http://127.0.0.1:8010/api/integrations
```

Expected: health remains `read_only` with opening disabled; the Oddpool public configuration, when present, returns only the canonical URL and a secret fingerprint, never the API key. Do not send a live Oddpool request without an operator-provided key.

- [ ] **Step 6: Commit documentation and final verification state**

```powershell
git add README.md
git commit -m "docs: explain fixed Oddpool discovery endpoint"
git status --short
```

Expected: the commit succeeds and the worktree is clean.
