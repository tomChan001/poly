# Live Trading Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local control plane operational after platform configuration by authenticating real accounts, running reviewed market pairs against native books, submitting real paired orders, and retaining execution history in PostgreSQL.

**Architecture:** Configuration remains in PostgreSQL and secrets remain in the operating-system credential store. A process-owned runtime reads enabled integrations, creates venue-specific authenticated transports, polls only human-reviewed `EXACT` pairs, evaluates native order books against the active risk policy, and sends the resulting authorization through the existing controlled execution state machine. Runtime status and immutable execution results are persisted and exposed read-only to the web UI.

**Tech Stack:** Python 3.13, FastAPI, httpx, cryptography, py-clob-client, SQLAlchemy/asyncpg, PostgreSQL, React 19, TypeScript, Vitest and Playwright.

---

### Task 1: Runtime configuration and authenticated readiness

**Files:**
- Modify: `backend/app/services/integration_config.py`
- Modify: `backend/app/adapters/integration_probe.py`
- Create: `backend/app/services/runtime_status.py`
- Create: `backend/app/api/routes/runtime.py`
- Test: `backend/tests/unit/services/test_runtime_configuration.py`
- Test: `backend/tests/integration/api/test_runtime_status.py`

- [ ] Write a failing test proving `runtime_bundle()` rejects missing, disabled, or incomplete Oddpool/Kalshi/Polymarket configuration and never exposes secrets through its public status view.
- [ ] Add an internal `RuntimeIntegration` value containing the record and credentials, plus `get_runtime()` and `runtime_bundle()` service methods.
- [ ] Replace public Kalshi/Polymarket probes with account-authenticated probes. Oddpool continues to use its authenticated bearer endpoint.
- [ ] Expose `GET /api/runtime` with `ready`, `running`, `last_cycle_at`, `last_error`, provider readiness, and the existing opening state. The response must not contain credential values.
- [ ] Run focused tests and confirm RED then GREEN.

### Task 2: Real venue transports

**Files:**
- Create: `backend/app/adapters/kalshi/http_transport.py`
- Create: `backend/app/adapters/polymarket/sdk_transport.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `backend/tests/unit/adapters/test_authenticated_transports.py`

- [ ] Write a failing Kalshi protocol test that verifies RSA-PSS/SHA-256 headers over `timestamp + method + path`, authenticated balance lookup, FOK order creation, and client-order-id recovery without logging the private key.
- [ ] Implement the async Kalshi transport with `cryptography` and `httpx`. Search recent orders by stable client order ID after an unknown response; never blindly resubmit.
- [ ] Write a failing Polymarket wrapper test around a fake SDK client proving FOK order signing/posting, matched-result normalization, balance allowance lookup, and known-order recovery.
- [ ] Implement a lazy `py-clob-client` wrapper using `asyncio.to_thread`; derive API credentials only when explicit API credentials are absent.
- [ ] Add `cryptography` and `py-clob-client` as locked runtime dependencies, then run adapter and existing execution fault tests.

### Task 3: Durable execution history

**Files:**
- Create: `migrations/versions/0003_runtime_execution_history.py`
- Create: `backend/app/db/executions.py`
- Modify: `backend/app/services/execution.py`
- Modify: `backend/app/api/routes/executions.py`
- Modify: `backend/app/container.py`
- Test: `backend/tests/integration/db/test_execution_history.py`

- [ ] Write a failing PostgreSQL test that saves a paired record with legs, fills and transitions, recreates the repository, and reads the same decimals and timestamps back.
- [ ] Add an `execution_record` table keyed by correlation ID with UTC occurrence time, state, and a structured JSONB snapshot. This table is the durable projection used by history; existing normalized ledger tables remain reserved for the full accounting ledger.
- [ ] Convert the execution store contract to async `save/list/get`; await persistence inside the state machine before returning a terminal result.
- [ ] Use the PostgreSQL store in `ApplicationContainer.runtime()` and keep the in-memory store in isolated unit containers.
- [ ] Make execution API handlers async and retain their existing response contract so the daily history page requires no fake data or duplicate storage.

### Task 4: Human-reviewed executable pairs

**Files:**
- Create: `migrations/versions/0004_executable_pairs.py`
- Create: `backend/app/services/executable_pairs.py`
- Create: `backend/app/db/executable_pairs.py`
- Create: `backend/app/api/routes/pairs.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/integration/api/test_executable_pairs.py`

- [ ] Write failing API tests for creating a pending pair with Kalshi ticker/outcome and Polymarket token/outcome, reviewing it as `EXACT`, and rejecting incomplete checklists or non-complementary truth tables.
- [ ] Persist stable market IDs, outcomes, source links, complete rule text/hash, minimum tick/quantity, settlement timestamps, status, review evidence, and enabled state.
- [ ] Reuse `REQUIRED_REVIEW_ITEMS`; only a local reviewer may set `EXACT`. Any material pair or rule edit returns the pair to `PENDING_REVIEW`.
- [ ] Expose list/create/update/review APIs. Runtime queries only enabled `EXACT` pairs.

### Task 5: Native-book automatic execution loop

**Files:**
- Create: `backend/app/adapters/native_market_data.py`
- Create: `backend/app/services/live_runtime.py`
- Modify: `backend/app/container.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/core/config.py`
- Test: `backend/tests/integration/services/test_live_runtime.py`

- [ ] Write a failing end-to-end service test with authenticated fake venues: one reviewed pair, fresh native books, adequate balances and profitable depth must create exactly one paired execution; repeating identical book sequences must not resubmit.
- [ ] Fetch Kalshi and Polymarket books directly from configured venue endpoints, normalize with existing parsers, and synchronize by age and arrival gap.
- [ ] Calculate the maximum quantity within per-trade policy and native depth. Reject non-EXACT, stale, unprofitable, insufficient-balance, duplicate-sequence, and disabled-opening cases with a structured runtime reason.
- [ ] Build a two-second authorization bound to pair rule hashes, book sequences, balance versions, quantity and FOK limits; execute through `ControlledExecutionService` with the real transports.
- [ ] Start one cancellable background task in FastAPI lifespan. Poll only when all integrations and at least one reviewed pair are ready; catch cycle errors into status without terminating the API.

### Task 6: Web configuration and operational status

**Files:**
- Modify: `frontend/src/api/client.ts`
- Create: `frontend/src/types/runtime.ts`
- Create: `frontend/src/pages/PairSettingsPage.tsx`
- Modify: `frontend/src/pages/MappingQueuePage.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.css`
- Test: `frontend/src/App.test.tsx`
- Test: `frontend/tests/runtime.spec.ts`

- [ ] Write failing frontend tests for a runtime status band, a pending pair audit form, and a persisted execution appearing under the correct Beijing history day.
- [ ] Show provider readiness and the exact blocking reason; never label the system ready merely because URLs were saved.
- [ ] Build pair creation/review controls with all checklist items and truth-table payouts. Human actions manage equivalence only; there is no per-order approval button.
- [ ] Refresh runtime, pairs, opportunities and history from real APIs. Display last cycle time and last failure without exposing secrets.
- [ ] Verify desktop and mobile geometry with Playwright screenshots.

### Task 7: Migration and completion verification

- [ ] Run Alembic through `0004` against the local PostgreSQL container.
- [ ] Run backend pytest, Ruff and mypy; run frontend Vitest, lint, build and Playwright.
- [ ] Restart the API, confirm `/health`, `/api/runtime`, `/api/pairs`, `/api/executions`, and the web UI.
- [ ] With no user credentials, report protocol-level test evidence separately from live sandbox verification; do not claim a real exchange order was placed.

### Self-Review

- Secrets cross only the internal runtime boundary and never appear in status, API payloads, logs, or persistence.
- Real submission still requires both `limited_auto` and the opening switch, plus an enabled `EXACT` pair.
- Duplicate book sequences and stable client order IDs prevent repeated submissions after retries or process uncertainty.
- History survives process restart because the API reads PostgreSQL, not process memory.
- User-requested Git handling is intentionally omitted.
