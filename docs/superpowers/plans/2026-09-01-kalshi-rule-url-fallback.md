# Kalshi Rule URL Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow Oddpool candidates with complete Kalshi rule text to pass native metadata normalization when Kalshi omits `rules_url`, without weakening rule validation or trading safety gates.

**Architecture:** Keep the compatibility rule inside the Kalshi adapter boundary. Normalize ticker and primary rule text as required non-empty strings, preserve a non-empty API-provided rule URL, and otherwise derive a display-only Kalshi market URL from the ticker. The pair resolver, native identifier checks, review workflow, and execution gates remain unchanged.

**Tech Stack:** Python 3.12, pytest, Pydantic domain models, httpx integration fixtures, Ruff, Mypy.

---

## File map

- Modify `backend/app/adapters/kalshi/markets.py`: validate required Kalshi text fields and derive the display URL when `rules_url` is absent.
- Modify `backend/tests/unit/adapters/test_market_metadata.py`: reproduce the missing-field failure and cover fallback, precedence, and fail-closed rule text behavior.
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

### Task 2: Verify the real candidate path and safety gates

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

Expected: the candidate no longer fails with `'rules_url'`. Other genuine identifier, settlement, or Polymarket metadata errors remain isolated.

- [ ] **Step 3: Restart the local backend and verify runtime status**

Restart port 8010 with the existing PostgreSQL connection, `TRADING_MODE=read_only`, `OPENING_ENABLED=false`, and `LOCAL_SETUP_ENABLED=true`. Verify:

```powershell
Invoke-RestMethod http://127.0.0.1:8010/health
Invoke-RestMethod http://127.0.0.1:8010/api/runtime/status
```

Expected: health is `ok`, trading mode remains `read_only`, opening remains disabled, and the runtime error no longer lists `'rules_url'` candidate failures.

- [ ] **Step 4: Confirm repository state**

```powershell
git diff --check
git status --short
git log -3 --oneline
```

Expected: no uncommitted implementation files and the adapter fix commit is present.
