# Polymarket Native Metadata Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the native pair resolver accept exact JSON decimal values and safely resolve named Polymarket outcomes through a uniquely cross-verified native token ID.

**Architecture:** Keep Gamma as the source of Polymarket rules, identifiers, minimum quantity, and minimum tick. Decode Gamma JSON through one exact-decimal boundary, validate numeric fields fail-closed, and prefer an Oddpool token ID only after it occurs exactly once in Gamma's native token list. Do not add CLOB requests or change execution gates.

**Tech Stack:** Python 3.13, `httpx`, `Decimal`, `pytest`, Ruff, Mypy

---

### Task 1: Decode and validate Gamma decimals exactly

**Files:**
- Modify: `backend/app/adapters/pair_metadata.py`
- Test: `backend/tests/unit/adapters/test_pair_metadata_resolver.py`

- [ ] **Step 1: Write the failing live-shape resolver test**

Add a resolver test whose Gamma response body contains JSON number literals rather than quoted
strings. Build the response from bytes so the test proves the production response decoder is used:

```python
@pytest.mark.asyncio
async def test_resolver_parses_gamma_json_decimals_exactly() -> None:
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
            content=(
                b'[{"question":"Will the event happen?",'
                b'"description":"Polymarket native rule",'
                b'"conditionId":"0xcondition",'
                b'"outcomes":"[\\"Yes\\", \\"No\\"]",'
                b'"clobTokenIds":"[\\"token-yes\\", \\"token-no\\"]",'
                b'"orderMinSize":5,"orderPriceMinTickSize":0.001}]'
            ),
            headers={"content-type": "application/json"},
        )

    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "oddpool:decimal:yes",
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

    assert pair.minimum_quantity == Decimal(5)
    assert pair.polymarket_minimum_tick == Decimal("0.001")
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_pair_metadata_resolver.py::test_resolver_parses_gamma_json_decimals_exactly -q
```

Expected: FAIL with `TypeError: Polymarket metadata is missing orderPriceMinTickSize` because
`response.json()` produced a float rejected by `_decimal_field()`.

- [ ] **Step 3: Add exact Gamma response decoding**

In `backend/app/adapters/pair_metadata.py`, keep Kalshi decoding unchanged and add a dedicated
Gamma decoder:

```python
def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Polymarket metadata contains invalid number: {value}")


def _gamma_payload(response: httpx.Response) -> object:
    return json.loads(
        response.text,
        parse_float=Decimal,
        parse_constant=_reject_json_constant,
    )
```

Use `_gamma_payload(polymarket_response)` for the markets response and
`_gamma_payload(event_response)` for the event fallback. Do not use this helper for Kalshi.

- [ ] **Step 4: Validate exact decimals with field-specific bounds**

Replace `_decimal_field()` with an implementation that distinguishes a missing field from an
invalid present value and accepts only exact numeric representations:

```python
def _decimal_field(
    payload: dict[str, object],
    *names: str,
    maximum: Decimal | None = None,
) -> Decimal:
    for name in names:
        if name not in payload:
            continue
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise TypeError(f"Polymarket {name} must be an exact decimal")
        try:
            parsed = value if isinstance(value, Decimal) else Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"Polymarket {name} is not a valid decimal") from exc
        if not parsed.is_finite():
            raise ValueError(f"Polymarket {name} must be finite")
        if parsed <= 0:
            raise ValueError(f"Polymarket {name} must be positive")
        if maximum is not None and parsed > maximum:
            raise ValueError(f"Polymarket {name} exceeds its maximum")
        return parsed
    raise TypeError(f"Polymarket metadata is missing {names[0]}")
```

Import `InvalidOperation` from `decimal`. Pass `maximum=Decimal(1)` only when reading
`orderPriceMinTickSize` / `minimum_tick_size`. Keep minimum order size unbounded but positive.

- [ ] **Step 5: Add numeric fail-closed tests**

Import `_decimal_field` and `_gamma_payload` from
`backend.app.adapters.pair_metadata`, then add these focused tests:

```python
@pytest.mark.parametrize("value", ["0.001", 1, Decimal("0.001")])
def test_polymarket_decimal_field_accepts_exact_positive_values(value: object) -> None:
    assert _decimal_field({"tick": value}, "tick", maximum=Decimal(1)) == Decimal(str(value))


@pytest.mark.parametrize("value", [True, 0.001])
def test_polymarket_decimal_field_rejects_inexact_types(value: object) -> None:
    with pytest.raises(TypeError, match="must be an exact decimal"):
        _decimal_field({"tick": value}, "tick", maximum=Decimal(1))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-0.001", "1.001"])
def test_polymarket_tick_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        _decimal_field({"tick": value}, "tick", maximum=Decimal(1))


def test_gamma_payload_rejects_non_standard_numeric_constants() -> None:
    request = httpx.Request("GET", "https://gamma.test/markets")
    response = httpx.Response(200, text='[{"orderPriceMinTickSize": NaN}]', request=request)
    with pytest.raises(ValueError, match="invalid number"):
        _gamma_payload(response)
```

- [ ] **Step 6: Run Task 1 tests and static checks**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_pair_metadata_resolver.py -q
uv run ruff check backend/app/adapters/pair_metadata.py backend/tests/unit/adapters/test_pair_metadata_resolver.py
uv run mypy backend/app/adapters/pair_metadata.py backend/tests/unit/adapters/test_pair_metadata_resolver.py
```

Expected: all tests pass; Ruff and Mypy report no issues.

- [ ] **Step 7: Commit Task 1**

```powershell
git add -- backend/app/adapters/pair_metadata.py backend/tests/unit/adapters/test_pair_metadata_resolver.py
git commit -m "fix: parse Polymarket metadata decimals exactly"
```

### Task 2: Cross-verify named-outcome token IDs

**Files:**
- Modify: `backend/app/adapters/pair_metadata.py`
- Test: `backend/tests/unit/adapters/test_pair_metadata_resolver.py`

- [ ] **Step 1: Write the failing named-outcome resolver test**

Add this resolver test with native outcomes `Mayweather` / `Pacquiao`, native token IDs
`token-mayweather` / `token-pacquiao`, logical Oddpool side `yes`, and a cross-checked source
token:

```python
@pytest.mark.asyncio
async def test_resolver_uses_cross_verified_token_for_named_polymarket_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "kalshi.test":
            return httpx.Response(
                200,
                json={
                    "market": {
                        "ticker": "KXBOXING",
                        "title": "Mayweather or Pacquiao?",
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
                    "question": "Mayweather or Pacquiao?",
                    "description": "Polymarket native rule",
                    "conditionId": "0xboxing",
                    "outcomes": '["Mayweather", "Pacquiao"]',
                    "clobTokenIds": '["token-mayweather", "token-pacquiao"]',
                    "orderMinSize": "5",
                    "orderPriceMinTickSize": "0.01",
                }
            ],
        )

    opportunity = OddpoolOpportunity.model_validate(
        {
            "id": "oddpool:boxing:mayweather",
            "title": "Mayweather or Pacquiao?",
            "outcome": "Mayweather",
            "updated_at": "2026-09-01T00:00:00Z",
            "gross_spread": "0.08",
            "estimated_fees": "0.03",
            "legs": [
                {
                    "venue": "kalshi",
                    "outcome": "no",
                    "market_ref": "KXBOXING",
                    "display_price": "0.69",
                },
                {
                    "venue": "polymarket",
                    "outcome": "yes",
                    "market_ref": "boxing-event",
                    "display_price": "0.32",
                    "source_condition_id": "0xboxing",
                    "source_token_id": "token-mayweather",
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

assert pair.polymarket_market_id == "token-mayweather"
assert pair.polymarket_outcome == "yes"
```

- [ ] **Step 2: Run the named-outcome test and verify RED**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_pair_metadata_resolver.py::test_resolver_uses_cross_verified_token_for_named_polymarket_outcome -q
```

Expected: FAIL with `ValueError: Polymarket outcome has no unique token: yes`.

- [ ] **Step 3: Add source token unique matching**

Extend `_polymarket_token()` and its caller:

```python
def _polymarket_token(
    payload: dict[str, object],
    outcome: str,
    expected_token_id: str | None = None,
) -> str:
    outcomes = _string_list(payload.get("outcomes"), "outcomes")
    token_ids = _string_list(payload.get("clobTokenIds"), "clobTokenIds")
    if len(outcomes) != len(token_ids):
        raise ValueError("Polymarket outcomes and token IDs do not align")
    if expected_token_id is not None:
        matches = [token for token in token_ids if token == expected_token_id]
        if len(matches) != 1:
            raise ValueError("Polymarket token ID must resolve to one native token")
        return matches[0]
    normalized = outcome.strip().lower()
    matches = [
        token
        for label, token in zip(outcomes, token_ids, strict=True)
        if label.lower() == normalized
    ]
    if len(matches) != 1:
        raise ValueError(f"Polymarket outcome has no unique token: {outcome}")
    return matches[0]
```

Call it with `polymarket_leg.source_token_id`. Preserve the existing later equality check as a
defensive assertion and do not change `polymarket_outcome`.

- [ ] **Step 4: Add token fail-closed and fallback tests**

Import `_polymarket_token` from `backend.app.adapters.pair_metadata` and add these cases:

```python
def test_polymarket_token_rejects_missing_expected_native_token() -> None:
    payload = {
        "outcomes": '["Mayweather", "Pacquiao"]',
        "clobTokenIds": '["token-mayweather", "token-pacquiao"]',
    }
    with pytest.raises(ValueError, match="must resolve to one native token"):
        _polymarket_token(payload, "yes", "unknown-token")


def test_polymarket_token_rejects_duplicate_expected_native_token() -> None:
    payload = {
        "outcomes": '["Mayweather", "Pacquiao"]',
        "clobTokenIds": '["token-shared", "token-shared"]',
    }
    with pytest.raises(ValueError, match="must resolve to one native token"):
        _polymarket_token(payload, "yes", "token-shared")


def test_polymarket_token_keeps_label_fallback_without_source_token() -> None:
    payload = {
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["token-yes", "token-no"]',
    }
    assert _polymarket_token(payload, "yes") == "token-yes"


def test_polymarket_token_rejects_misaligned_native_arrays() -> None:
    payload = {
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["token-yes"]',
    }
    with pytest.raises(ValueError, match="do not align"):
        _polymarket_token(payload, "yes", "token-yes")
```

Update the existing native-ID mismatch parameter so an unknown source token expects
`Polymarket token ID must resolve to one native token`; keep the condition-ID mismatch case
unchanged.

- [ ] **Step 5: Run all related tests and static checks**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_pair_metadata_resolver.py backend/tests/unit/adapters/test_market_metadata.py backend/tests/unit/adapters/test_oddpool.py -q
uv run ruff check backend/app/adapters/pair_metadata.py backend/tests/unit/adapters/test_pair_metadata_resolver.py
uv run mypy backend/app/adapters/pair_metadata.py backend/tests/unit/adapters/test_pair_metadata_resolver.py
```

Expected: all tests pass; Ruff and Mypy report no issues.

- [ ] **Step 6: Commit Task 2**

```powershell
git add -- backend/app/adapters/pair_metadata.py backend/tests/unit/adapters/test_pair_metadata_resolver.py
git commit -m "fix: verify Polymarket native token IDs"
```

### Task 3: Verify real candidates and safety gates

**Files:**
- Verify only; do not modify production files.

- [ ] **Step 1: Probe the four real candidates read-only**

Use `KeyringSecretStore(settings.credential_service_name)` to load the already-saved Oddpool token
without printing it. Use a fresh `httpx.AsyncClient`, `OddpoolClient`, and
`NativePairMetadataResolver` from this worktree. Resolve these exact IDs:

```text
oddpool:floyd-mayweather-vs-manny-pacquiao-2:floyd_mayweather_jr
oddpool:israel-pm-2045:gadi_eisenkot
oddpool:brazil-senate-election-most-seats-2026:mdb
oddpool:gop-nominee-2028:jd_vance
```

For each, report only public market IDs, public rule URL, minimum ticks, minimum quantity,
quantity step, and success/error type. Never print credentials. Expected: all four resolve; no
error contains `rules_url`, `tick_size`, `minimum_order_size`,
`orderPriceMinTickSize`, or `outcome has no unique token`.

- [ ] **Step 2: Run the backend full suite with the test Postgres**

Derive `DATABASE_URL` from the running `controlled-execution-postgres-alt` container in one
PowerShell process without printing its environment, then run:

```powershell
uv run pytest -q
```

Expected: all tests pass, with only the repository's documented skip.

- [ ] **Step 3: Run final static verification**

```powershell
uv run ruff check backend migrations
uv run mypy backend/app/adapters/pair_metadata.py backend/app/adapters/kalshi/markets.py backend/app/adapters/oddpool/schema.py backend/app/services/pair_discovery.py backend/tests/unit/adapters/test_pair_metadata_resolver.py backend/tests/unit/adapters/test_market_metadata.py
git diff --check
git status --short
```

Expected: Ruff and Mypy pass; diff check emits no output; worktree is clean.

- [ ] **Step 4: Confirm safety remains unchanged**

Confirm through tests and the branch diff that defaults remain `TradingMode.READ_ONLY` and
`opening_enabled=False`, resolver results remain pending-review drafts, and no order submission
path changed. Do not restart or mutate the current 8010 instance until after the branch is merged.
