# Kalshi Market Metadata Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow Oddpool candidates with complete Kalshi rule text to pass native metadata normalization when current Kalshi responses omit `rules_url`, `tick_size`, and `minimum_order_size`, without weakening rule validation or trading safety gates.

**Architecture:** Keep all response-shape compatibility inside the Kalshi adapter boundary. Normalize ticker and primary rule text as required non-empty strings, derive a display-only URL when needed, prefer the legacy tick when present, otherwise validate `price_ranges` and store its minimum positive step, and use one whole contract when the current response omits a minimum order size. The pair resolver, native identifier checks, review workflow, whole-contract policy, and execution gates remain unchanged.

**Tech Stack:** Python 3.12, pytest, Pydantic domain models, httpx integration fixtures, Ruff, Mypy.

---

## File map

- Modify `backend/app/adapters/kalshi/markets.py`: validate required Kalshi text fields, derive a display URL, normalize legacy/current tick metadata, and apply the whole-contract minimum.
- Modify `backend/tests/unit/adapters/test_market_metadata.py`: cover link fallback, legacy/current field precedence, valid price ranges, invalid ranges, and fail-closed rule behavior.
- Verify `backend/tests/unit/adapters/test_pair_metadata_resolver.py`: ensure native metadata resolution still uses Kalshi rules and identifier checks.
- Verify `backend/tests/integration/services/test_pair_discovery.py`: ensure candidate isolation and review-draft creation remain intact.

### Task 1: Normalize Kalshi markets without a required `rules_url`

**Files:**
- Modify: `backend/app/adapters/kalshi/markets.py`
- Test: `backend/tests/unit/adapters/test_market_metadata.py`

- [ ] **Step 1: Add the missing-`rules_url` regression test**

Add a focused test using the current Kalshi response shape:

```python
def test_kalshi_market_derives_rule_url_when_api_omits_it() -> None:
    market = normalize_kalshi_market(
        {
            "ticker": "KXBOXING-26SEP19FMAYMPAC-FMAY",
            "title": "Floyd Mayweather Jr. wins?",
            "status": "open",
            "rules_primary": "Resolves yes if Floyd Mayweather Jr. wins.",
            "rules_secondary": "Official settlement controls.",
            "tick_size": "0.01",
            "minimum_order_size": "1",
        }
    )

    assert market.rule_text == "Resolves yes if Floyd Mayweather Jr. wins."
    assert market.rule_url == (
        "https://kalshi.com/markets/KXBOXING-26SEP19FMAYMPAC-FMAY"
    )
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_market_metadata.py::test_kalshi_market_derives_rule_url_when_api_omits_it -q
```

Expected: FAIL with `KeyError: 'rules_url'`, proving the test reproduces the reported production failure.

- [ ] **Step 3: Add precedence and fail-closed rule tests**

Extend the existing Kalshi test to assert an API-provided URL is preserved:

```python
assert market.rule_url == "https://kalshi.com/markets/KX-EXAMPLE"
```

Add required rule-text coverage:

```python
@pytest.mark.parametrize("rules_primary", [None, "", "   "])
def test_kalshi_market_rejects_missing_or_blank_primary_rules(
    rules_primary: object,
) -> None:
    payload = {
        "ticker": "KX-EXAMPLE",
        "title": "Example?",
        "status": "open",
        "rules_primary": rules_primary,
        "tick_size": "0.01",
        "minimum_order_size": "1",
    }

    with pytest.raises(TypeError, match="Kalshi rules_primary must be non-empty text"):
        normalize_kalshi_market(payload)
```

Import `pytest` in the test file.

- [ ] **Step 4: Implement the minimal adapter fallback**

In `backend/app/adapters/kalshi/markets.py`, normalize the two required text values before constructing `MarketMetadata`:

```python
def normalize_kalshi_market(payload: dict[str, Any]) -> MarketMetadata:
    ticker = _required_text(payload, "ticker")
    rule_text = _required_text(payload, "rules_primary")
    raw_rule_url = payload.get("rules_url")
    rule_url = (
        raw_rule_url.strip()
        if isinstance(raw_rule_url, str) and raw_rule_url.strip()
        else f"https://kalshi.com/markets/{ticker}"
    )
    return MarketMetadata(
        venue=Venue.KALSHI,
        external_id=ticker,
        title=str(payload["title"]),
        status=str(payload["status"]),
        outcomes=("yes", "no"),
        rule_text=rule_text,
        rule_url=rule_url,
        minimum_tick=parse_price(str(payload["tick_size"])),
        minimum_quantity=parse_decimal(str(payload["minimum_order_size"])),
        raw_payload=payload,
    )


def _required_text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Kalshi {name} must be non-empty text")
    return value.strip()
```

Do not add fallback rule text, consume Oddpool rule fields, or alter resolver/execution behavior.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_market_metadata.py -q
uv run pytest backend/tests/unit/adapters/test_pair_metadata_resolver.py backend/tests/integration/services/test_pair_discovery.py -q
uv run ruff check backend/app/adapters/kalshi/markets.py backend/tests/unit/adapters/test_market_metadata.py
uv run mypy backend/app/adapters/kalshi/markets.py backend/tests/unit/adapters/test_market_metadata.py
```

Expected: all tests pass, Ruff reports `All checks passed!`, and Mypy reports no issues.

- [ ] **Step 6: Commit the adapter fix**

```powershell
git add backend/app/adapters/kalshi/markets.py backend/tests/unit/adapters/test_market_metadata.py
git commit -m "fix: tolerate missing Kalshi rule URLs"
```

### Task 2: Normalize Kalshi fixed-point tick and quantity metadata

**Files:**
- Modify: `backend/app/adapters/kalshi/markets.py`
- Test: `backend/tests/unit/adapters/test_market_metadata.py`
- Test: `backend/tests/unit/adapters/test_pair_metadata_resolver.py`

- [ ] **Step 1: Add a current-response regression test**

Add a payload that matches the verified live Kalshi shape: no `tick_size`, no `minimum_order_size`, and one `price_ranges` entry.

```python
def test_kalshi_market_uses_current_price_ranges_and_whole_contract_default() -> None:
    market = normalize_kalshi_market(
        {
            "ticker": "KXBOXING-26SEP19FMAYMPAC-FMAY",
            "title": "Will Floyd Mayweather beat Manny Pacquiao?",
            "status": "open",
            "rules_primary": "Resolves yes if Floyd Mayweather wins the bout.",
            "price_level_structure": "linear_cent",
            "price_ranges": [
                {"start": "0.0000", "end": "1.0000", "step": "0.0100"}
            ],
        }
    )

    assert market.minimum_tick == Decimal("0.0100")
    assert market.minimum_quantity == Decimal(1)
```

- [ ] **Step 2: Run the current-response test and verify RED**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_market_metadata.py::test_kalshi_market_uses_current_price_ranges_and_whole_contract_default -q
```

Expected: FAIL with `KeyError: 'tick_size'`, reproducing the second live failure discovered after the URL fix.

- [ ] **Step 3: Add range, precedence, and quantity edge tests**

Add a tapered-range test and assert the minimum positive step is selected:

```python
def test_kalshi_market_uses_smallest_valid_price_range_step() -> None:
    payload = current_kalshi_payload()
    payload["price_ranges"] = [
        {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
        {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
        {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
    ]

    assert normalize_kalshi_market(payload).minimum_tick == Decimal("0.0010")
```

Keep the existing legacy fixture and add a malformed `price_ranges` value to it, proving a valid legacy `tick_size` has precedence. Assert the legacy `minimum_order_size` is also preserved.

Add parametrized invalid current ranges:

```python
@pytest.mark.parametrize(
    "price_ranges",
    [
        [],
        [{"start": "0", "end": "1", "step": "0"}],
        [{"start": "0.8", "end": "0.2", "step": "0.01"}],
        [{"start": "-0.1", "end": "1", "step": "0.01"}],
        [{"start": "0", "end": "1.1", "step": "0.01"}],
        [{"start": "0", "end": "0.1", "step": "0.2"}],
        [{"start": "bad", "end": "1", "step": "0.01"}],
    ],
)
def test_kalshi_market_rejects_invalid_price_ranges(
    price_ranges: object,
) -> None:
    payload = current_kalshi_payload()
    payload["price_ranges"] = price_ranges

    with pytest.raises((TypeError, ValueError)):
        normalize_kalshi_market(payload)
```

Add a separate parametrized test for present-but-invalid legacy `minimum_order_size` values `0`, `-1`, and `"bad"`; all must raise instead of silently using the default. Extract `current_kalshi_payload()` as a test-only fixture function returning a fresh dictionary.

- [ ] **Step 4: Implement strict legacy/current metadata parsing**

Change the `MarketMetadata` construction to use two private helpers:

```python
minimum_tick=_minimum_tick(payload),
minimum_quantity=_minimum_quantity(payload),
```

Implement:

```python
def _minimum_tick(payload: dict[str, Any]) -> Decimal:
    legacy_tick = payload.get("tick_size")
    if legacy_tick is not None:
        tick = parse_price(legacy_tick)
        if tick <= 0:
            raise ValueError("Kalshi tick_size must be positive")
        return tick

    ranges = payload.get("price_ranges")
    if not isinstance(ranges, list) or not ranges:
        raise TypeError("Kalshi price_ranges must be a non-empty list")
    steps: list[Decimal] = []
    for price_range in ranges:
        if not isinstance(price_range, dict):
            raise TypeError("Kalshi price_ranges entries must be objects")
        start = parse_price(price_range.get("start"))
        end = parse_price(price_range.get("end"))
        step = parse_price(price_range.get("step"))
        if start >= end:
            raise ValueError("Kalshi price range start must be below end")
        if step <= 0 or step > end - start:
            raise ValueError("Kalshi price range step is invalid")
        steps.append(step)
    return min(steps)


def _minimum_quantity(payload: dict[str, Any]) -> Decimal:
    raw_value = payload.get("minimum_order_size")
    if raw_value is None:
        return Decimal(1)
    value = parse_decimal(raw_value)
    if value <= 0:
        raise ValueError("Kalshi minimum_order_size must be positive")
    return value
```

Import `Decimal`. Do not infer fractional execution from `*_fp` market fields and do not change `NativePairMetadataResolver.quantity_step`.

- [ ] **Step 5: Update the resolver current-response regression**

In the existing resolver regression added for missing `rules_url`, replace the legacy `tick_size` and `minimum_order_size` with the live `price_level_structure` and `price_ranges` fields. Keep `OddpoolLeg.market_url=None`. Assert:

```python
assert pair.kalshi_minimum_tick == Decimal("0.01")
assert pair.minimum_quantity >= Decimal(1)
assert pair.quantity_step == Decimal(1)
```

- [ ] **Step 6: Run focused checks and verify GREEN**

Run:

```powershell
uv run pytest backend/tests/unit/adapters/test_market_metadata.py backend/tests/unit/adapters/test_pair_metadata_resolver.py backend/tests/integration/services/test_pair_discovery.py -q
uv run ruff check backend/app/adapters/kalshi/markets.py backend/tests/unit/adapters/test_market_metadata.py backend/tests/unit/adapters/test_pair_metadata_resolver.py
uv run mypy backend/app/adapters/kalshi/markets.py backend/app/adapters/pair_metadata.py backend/tests/unit/adapters/test_market_metadata.py backend/tests/unit/adapters/test_pair_metadata_resolver.py
```

Expected: all tests pass, Ruff reports `All checks passed!`, and Mypy reports no issues.

- [ ] **Step 7: Commit the fixed-point compatibility change**

```powershell
git add backend/app/adapters/kalshi/markets.py backend/tests/unit/adapters/test_market_metadata.py backend/tests/unit/adapters/test_pair_metadata_resolver.py
git commit -m "fix: support current Kalshi market increments"
```

### Task 3: Verify the real candidate path and safety gates

**Files:**
- Verify only; no additional production changes expected.

- [ ] **Step 1: Run the backend regression suite**

Use the running local PostgreSQL test container to provide `DATABASE_URL`, then run:

```powershell
uv run pytest -q
uv run ruff check backend migrations
uv run mypy backend/app/adapters/kalshi/markets.py backend/app/adapters/pair_metadata.py backend/app/services/pair_discovery.py
```

Expected: the backend suite passes with only the existing destructive-database skip, Ruff passes, and Mypy reports no issues.

- [ ] **Step 2: Re-run one real Oddpool-to-native resolution cycle safely**

Use the saved Oddpool Key through `KeyringSecretStore`, fetch current candidates read-only, and resolve at least one previously failing candidate through `NativePairMetadataResolver`. Print only candidate ID and success/error type; never print credentials or full upstream response bodies.

Expected: the candidate no longer fails with `'rules_url'`, `'tick_size'`, or `'minimum_order_size'`. Other genuine identifier, settlement, or Polymarket metadata errors remain isolated.

- [ ] **Step 3: Restart the local backend and verify runtime status**

Restart port 8010 with the existing PostgreSQL connection, `TRADING_MODE=read_only`, `OPENING_ENABLED=false`, and `LOCAL_SETUP_ENABLED=true`. Verify:

```powershell
Invoke-RestMethod http://127.0.0.1:8010/health
Invoke-RestMethod http://127.0.0.1:8010/api/runtime/status
```

Expected: health is `ok`, trading mode remains `read_only`, opening remains disabled, and the runtime error no longer lists `'rules_url'`, `'tick_size'`, or `'minimum_order_size'` candidate failures.

- [ ] **Step 4: Confirm repository state**

```powershell
git diff --check
git status --short
git log -3 --oneline
```

Expected: no uncommitted implementation files and the adapter fix commit is present.
