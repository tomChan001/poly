# Polymarket Magic Authentication and Pre-Live Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support Google/email-created Polymarket proxy wallets on CLOB V2 and complete the fail-closed trading logic that can be verified without placing a real order.

**Architecture:** Normalize every Polymarket account into an owner, funder, signature type, and credential bundle before a transport is created. Refactor runtime evaluation into persisted, structured evidence that composes native metadata, fee-aware depth, per-venue capital, operational gates, reconciliation, and incident notification.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy/Alembic, PostgreSQL, `py-clob-client-v2`, `eth-account`, pytest, React 19, TypeScript, Vitest, Playwright.

---

### Task 1: Magic/proxy account normalization

**Files:**
- Create: `backend/app/adapters/polymarket/account.py`
- Modify: `backend/app/services/integration_config.py`
- Test: `backend/tests/unit/adapters/test_polymarket_account.py`
- Test: `backend/tests/unit/services/test_runtime_configuration.py`

- [ ] **Step 1: Write failing account-normalization tests**

```python
def test_magic_proxy_derives_owner_and_signature_type(private_key: str) -> None:
    resolver = PolymarketAccountResolver(profile_lookup=profile_lookup("0xproxy"))
    profile = await resolver.resolve("magic_proxy", private_key, None, 137)
    assert profile.signature_type == 1
    assert profile.funder_address == "0xproxy"

def test_magic_proxy_rejects_mismatched_funder(private_key: str) -> None:
    with pytest.raises(PolymarketAccountError, match="FUNDER_MISMATCH"):
        await resolver.resolve("magic_proxy", private_key, "0xwrong", 137)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest backend/tests/unit/adapters/test_polymarket_account.py -q`

Expected: collection/import failure because `PolymarketAccountResolver` does not exist.

- [ ] **Step 3: Implement the normalized account boundary**

```python
class PolymarketAccountType(StrEnum):
    MAGIC_PROXY = "magic_proxy"
    GNOSIS_SAFE = "gnosis_safe"
    DEPOSIT_WALLET = "deposit_wallet"
    EOA = "eoa"

@dataclass(frozen=True, slots=True)
class PolymarketAccountProfile:
    account_type: PolymarketAccountType
    owner_address: str
    funder_address: str
    signature_type: int
    chain_id: int
```

Use `eth_account.Account.from_key` for owner derivation. `MAGIC_PROXY` always resolves signature type 1 and obtains its proxy from the injected public-profile lookup. Reject owner/funder/signature mismatches with stable codes.

- [ ] **Step 4: Extend integration-field validation**

Add `account_type` and `owner_address` to Polymarket public configuration. Require `account_type`, `funder_address`, `signature_type`, and `chain_id`; continue requiring `private_key`. Reject partial API credentials unless `api_key`, `api_secret`, and `passphrase` are all present.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `uv run pytest backend/tests/unit/adapters/test_polymarket_account.py backend/tests/unit/services/test_runtime_configuration.py -q`

Expected: all selected tests pass.

### Task 2: Upgrade the Polymarket transport to CLOB V2

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `backend/app/adapters/polymarket/sdk_transport.py`
- Modify: `backend/app/container.py`
- Test: `backend/tests/unit/adapters/test_authenticated_transports.py`

- [ ] **Step 1: Write failing V2 transport contract tests**

```python
async def test_v2_transport_uses_magic_signature_and_real_trade_fills() -> None:
    transport = PolymarketSdkTransport(fake_v2_client, ...)
    result = await transport.create_order(payload)
    assert fake_v2_client.signature_type == 1
    assert result["fills"] == [{"id": "trade-1", "size": "3", "price": "0.47", "fee": "0.02"}]

async def test_v2_transport_can_recover_from_recent_trades() -> None:
    recovered = await transport.get_order_by_client_id("stable-client-id")
    assert recovered["clientOrderId"] == "stable-client-id"
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest backend/tests/unit/adapters/test_authenticated_transports.py -q`

Expected: failures show the V1 constructor and fabricated requested-price fills.

- [ ] **Step 3: Replace the V1 dependency and adapter**

Replace `py-clob-client` with `py-clob-client-v2`. Adapt `from_credentials` to the V2 constructor, derive L2 credentials only when a complete set is absent, and normalize fills from trade/order queries rather than requested values.

- [ ] **Step 4: Update container construction**

Build the V2 transport from normalized `account_type`, `owner_address`, `funder_address`, `signature_type`, and `chain_id`. Cache keys must include integration record version so credential changes recreate the client.

- [ ] **Step 5: Run adapter tests and type checks**

Run: `uv run pytest backend/tests/unit/adapters/test_authenticated_transports.py backend/tests/unit/adapters/test_trading_adapters.py -q`

Run: `uv run mypy backend/app/adapters/polymarket backend/app/container.py`

Expected: all commands exit 0.

### Task 3: Read-only Polymarket connection test and guided UI

**Files:**
- Modify: `backend/app/adapters/integration_probe.py`
- Modify: `backend/app/services/integration_config.py`
- Modify: `frontend/src/pages/IntegrationSettingsPage.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/tests/opportunities.spec.ts`
- Test: `backend/tests/integration/api/test_integrations.py`

- [ ] **Step 1: Write failing API and UI tests**

```python
async def test_magic_connection_probe_reads_balance_without_posting_order() -> None:
    result = await probe.test(record, secrets)
    assert result.ok is True
    assert fake_client.posted_orders == []
```

```tsx
expect(screen.getByLabelText('账户类型')).toHaveValue('magic_proxy')
expect(screen.getByText('测试连接（不会下单）')).toBeInTheDocument()
expect(screen.queryByLabelText('Google 密码')).not.toBeInTheDocument()
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest backend/tests/integration/api/test_integrations.py -q`

Run: `cd frontend && npm run test -- --run`

- [ ] **Step 3: Implement read-only probing**

Resolve the account, authenticate/derive L2 credentials, read collateral balance and allowance, and query recoverable orders/trades. Return stable sanitized detail codes and never call an order-write method.

- [ ] **Step 4: Implement the guided Magic configuration panel**

Add account-type selection, official export link, write-only private-key field, derived-address display, and advanced account modes. Signature type is displayed but not editable. Preserve existing secret fingerprints.

- [ ] **Step 5: Verify API, unit UI, and browser UI**

Run: `uv run pytest backend/tests/integration/api/test_integrations.py -q`

Run: `cd frontend && npm run test && npm exec playwright test tests/opportunities.spec.ts`

Expected: all commands exit 0 and no test transport records an order write.

### Task 4: Preserve settlement and invalidate changed native rules

**Files:**
- Modify: `backend/app/services/executable_pairs.py`
- Modify: `backend/app/adapters/pair_metadata.py`
- Modify: `backend/app/db/executable_pairs.py`
- Create: `migrations/versions/0005_prelive_safety.py`
- Test: `backend/tests/unit/adapters/test_pair_metadata_resolver.py`
- Test: `backend/tests/integration/services/test_pair_discovery.py`
- Test: `backend/tests/integration/db/test_executable_pair_repository.py`

- [ ] **Step 1: Write failing settlement and fingerprint regressions**

```python
async def test_native_rule_change_invalidates_exact_without_oddpool_timestamp_change() -> None:
    await discover(rule="v1", source_time=NOW)
    await review_exact()
    result = await discover(rule="v2", source_time=NOW)
    assert result.updated == 1
    assert (await pairs.list())[0].status is MappingStatus.PENDING_REVIEW

def test_resolver_preserves_native_worst_case_settlement() -> None:
    assert pair.worst_case_settlement_at == EXPECTED_SETTLEMENT
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest backend/tests/integration/services/test_pair_discovery.py backend/tests/unit/adapters/test_pair_metadata_resolver.py -q`

- [ ] **Step 3: Add execution-critical metadata and fingerprinting**

Extend `ExecutablePairInput` with settlement timestamps, category, tick, and native fingerprint. Compute the fingerprint from normalized material fields. `upsert_discovered` compares fingerprints before declaring a duplicate and resets changed pairs to pending review.

- [ ] **Step 4: Persist the added metadata**

Update the JSON projection and migration-compatible repository. Run Alembic upgrade/downgrade coverage against the temporary PostgreSQL database.

- [ ] **Step 5: Verify focused tests**

Run: `uv run pytest backend/tests/integration/services/test_pair_discovery.py backend/tests/unit/adapters/test_pair_metadata_resolver.py backend/tests/integration/db/test_executable_pair_repository.py -q`

### Task 5: Fee-aware depth and per-venue capital limits

**Files:**
- Modify: `backend/app/services/optimizer.py`
- Modify: `backend/app/services/live_runtime.py`
- Modify: `backend/app/services/capital.py`
- Modify: `backend/app/container.py`
- Test: `backend/tests/unit/services/test_optimizer.py`
- Test: `backend/tests/unit/services/test_capital.py`
- Test: `backend/tests/integration/services/test_live_runtime.py`

- [ ] **Step 1: Write failing regressions**

```python
def test_optimizer_rejects_quantity_whose_swept_cost_exceeds_one_venue_balance() -> None:
    result = optimizer.optimize(..., kalshi_balance=Decimal("5"))
    assert result.best_quote.quantity < Decimal("10")

def test_unknown_fee_category_fails_closed() -> None:
    assert optimizer.optimize(...).rejection_reasons == ("FEE_UNKNOWN",)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest backend/tests/unit/services/test_optimizer.py backend/tests/integration/services/test_live_runtime.py -q`

- [ ] **Step 3: Extend optimization inputs and rejection behavior**

Quote every cumulative depth breakpoint using swept per-venue cost and fee. Enforce both balances plus trade/event/portfolio limits. Catch missing fee rules as `FEE_UNKNOWN`; never substitute zero.

- [ ] **Step 4: Wire capital reservation before authorization**

Reserve the selected Kalshi and Polymarket amounts atomically using the stable correlation ID. Put the real reservation ID in `ExecutionEvidence`; release or convert the reservation on terminal execution outcomes.

- [ ] **Step 5: Verify optimizer, capital, and live runtime**

Run: `uv run pytest backend/tests/unit/services/test_optimizer.py backend/tests/unit/services/test_capital.py backend/tests/integration/services/test_live_runtime.py -q`

### Task 6: Structured rejected evaluations and settlement gates

**Files:**
- Modify: `backend/app/services/live_runtime.py`
- Modify: `backend/app/services/opportunities.py`
- Modify: `backend/app/api/routes/opportunities.py`
- Modify: `frontend/src/types/opportunity.ts`
- Modify: `frontend/src/pages/OpportunitiesPage.tsx`
- Test: `backend/tests/integration/api/test_opportunities.py`
- Test: `backend/tests/integration/services/test_live_runtime.py`
- Test: `frontend/src/App.test.tsx`

- [ ] **Step 1: Write failing rejection-evidence tests**

```python
async def test_late_settlement_is_retained_as_structured_rejection() -> None:
    await runtime.run_once(NOW)
    [record] = opportunities.list_ranked()
    assert record.rejection_reasons == ("SETTLEMENT_TOO_LATE",)

async def test_stale_book_rejection_is_visible_through_api() -> None:
    assert response.json()[0]["rejection_reasons"] == ["STALE_BOOK"]
```

- [ ] **Step 2: Run focused backend tests and verify RED**

Run: `uv run pytest backend/tests/integration/services/test_live_runtime.py backend/tests/integration/api/test_opportunities.py -q`

- [ ] **Step 3: Return an evaluation for every pair**

Replace `None` exits with immutable rejected records carrying all accumulated codes and evidence versions. Enforce worst-case settlement against policy before balances or order submission.

- [ ] **Step 4: Display structured reasons in the UI**

Render readable reason labels, book age, settlement timestamp, and fee status for rejected opportunities.

- [ ] **Step 5: Verify backend and frontend tests**

Run: `uv run pytest backend/tests/integration/services/test_live_runtime.py backend/tests/integration/api/test_opportunities.py -q`

Run: `cd frontend && npm run test`

### Task 7: Persist control state and gate runtime startup

**Files:**
- Create: `backend/app/db/operational_control.py`
- Modify: `backend/app/services/system_control.py`
- Modify: `backend/app/services/settings.py`
- Modify: `backend/app/container.py`
- Modify: `backend/app/main.py`
- Modify: `migrations/versions/0005_prelive_safety.py`
- Test: `backend/tests/integration/db/test_postgres_runtime.py`
- Test: `backend/tests/integration/api/test_kill_switch.py`
- Test: `backend/tests/unit/test_live_runtime_loop.py`

- [ ] **Step 1: Write failing restart and startup-gate tests**

```python
async def test_disabled_opening_survives_container_recreation() -> None:
    await control.disable_opening("incident")
    recreated = await load_control()
    assert recreated.opening_enabled is False

async def test_runtime_startup_closes_opening_when_evidence_is_missing() -> None:
    await start_runtime(mode="limited_auto", opening=True, evidence=None)
    assert control.opening_enabled is False
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest backend/tests/integration/api/test_kill_switch.py backend/tests/unit/test_live_runtime_loop.py -q`

- [ ] **Step 3: Add PostgreSQL repositories for controls and risk policy**

Persist opening state/reason/version and immutable risk policy versions. Expose async service methods so every state change is committed before API success.

- [ ] **Step 4: Apply the automation gate during startup**

Before scheduling live cycles, evaluate persisted evidence. Missing/failed evidence disables opening and records reasons. Fresh-install defaults become read-only and closed.

- [ ] **Step 5: Verify restart and migration behavior**

Run: `uv run pytest backend/tests/integration/db/test_postgres_runtime.py backend/tests/integration/api/test_kill_switch.py backend/tests/unit/test_live_runtime_loop.py -q`

### Task 8: Reconciliation, incidents, simulated emergency action, and outbox

**Files:**
- Create: `backend/app/services/execution_supervisor.py`
- Create: `backend/app/db/incidents.py`
- Create: `backend/app/db/outbox.py`
- Modify: `backend/app/services/execution.py`
- Modify: `backend/app/services/notifications.py`
- Modify: `backend/app/container.py`
- Modify: `migrations/versions/0005_prelive_safety.py`
- Test: `backend/tests/integration/services/test_execution_faults.py`
- Test: `backend/tests/unit/services/test_emergency_hedge.py`
- Test: `backend/tests/unit/services/test_notifications.py`

- [ ] **Step 1: Write failing supervisor tests**

```python
async def test_partial_fill_persists_incident_and_simulated_emergency_action() -> None:
    record = await supervisor.finalize(partial_execution, mode=TradingMode.SHADOW)
    assert record.state is ExecutionState.PARTIALLY_HEDGED
    assert incidents[0].action == "simulate_hedge"
    assert outbox[0].event_type == "execution.partially_hedged"
    assert ports.total_submissions == 0
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest backend/tests/integration/services/test_execution_faults.py backend/tests/unit/services/test_emergency_hedge.py backend/tests/unit/services/test_notifications.py -q`

- [ ] **Step 3: Implement the execution supervisor**

Reconcile fills/orders, compare actual/estimated fees, transition state, persist an incident, close opening, and enqueue one idempotent outbox event. Shadow/read-only emergency actions produce decisions only and never call a trading port.

- [ ] **Step 4: Persist incidents and outbox events**

Use correlation/event keys for idempotent inserts. Do not store credential fields in payload JSON.

- [ ] **Step 5: Verify the fault matrix**

Run: `uv run pytest backend/tests/integration/services/test_execution_faults.py backend/tests/unit/services/test_emergency_hedge.py backend/tests/unit/services/test_notifications.py -q`

### Task 9: Native-book freshness and sequence safety

**Files:**
- Modify: `backend/app/adapters/native_market_data.py`
- Modify: `backend/app/services/orderbooks.py`
- Modify: `backend/app/services/settings.py`
- Test: `backend/tests/unit/adapters/test_native_market_data.py`
- Test: `backend/tests/unit/services/test_orderbooks.py`

- [ ] **Step 1: Write failing freshness and sequence tests**

```python
def test_missing_venue_capture_time_is_not_relabelled_as_now() -> None:
    with pytest.raises(BookSynchronizationError, match="freshness evidence"):
        synchronize_books(book_without_capture_time, ...)

def test_sequence_gap_remains_unsafe_until_snapshot_replace() -> None:
    state.accept_sequence(12)
    assert state.safety is BookSafety.UNSAFE
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run pytest backend/tests/unit/adapters/test_native_market_data.py backend/tests/unit/services/test_orderbooks.py -q`

- [ ] **Step 3: Preserve venue time and separate synchronization limits**

Do not use evaluation time as venue capture time. Add `maximum_arrival_gap_seconds` with a 0.5-second default and retain the independent maximum-age policy.

- [ ] **Step 4: Wire sequence state into evaluation**

Reject unsafe sequence state until a REST snapshot replacement establishes a new baseline. Hash-only books remain deduplicatable but cannot count toward sequence-continuity acceptance.

- [ ] **Step 5: Verify focused tests**

Run: `uv run pytest backend/tests/unit/adapters/test_native_market_data.py backend/tests/unit/services/test_orderbooks.py -q`

### Task 10: Full verification and operational documentation

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `docs/runbooks/credential-rotation.md`
- Modify: `docs/runbooks/partially-hedged.md`
- Modify: `docs/acceptance/read-only.md`

- [ ] **Step 1: Document Magic setup and safe defaults**

Document the official export URL, proxy-wallet discovery, OS credential-store boundary, connection test, V2 dependency, closed startup behavior, and explicit statement that verification sends no real order.

- [ ] **Step 2: Run database migration verification**

Run: `uv run alembic upgrade head`

Run: `uv run pytest backend/tests/integration/db -q`

- [ ] **Step 3: Run the full backend gate**

Run: `uv run pytest -q`

Run: `uv run ruff check .`

Run: `uv run mypy backend/app`

Expected: all commands exit 0 with no skipped required PostgreSQL checks.

- [ ] **Step 4: Run the full frontend gate**

Run: `cd frontend && npm run test && npm run lint && npm run build && npm exec playwright test`

Expected: all commands exit 0.

- [ ] **Step 5: Confirm no live order was sent**

Inspect test transport counters and runtime configuration. Record protocol-level evidence separately from live-canary evidence; leave live-canary status unverified.

