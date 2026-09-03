# Unified Real Ordering Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one durable Integrations-page switch the only manual permission for new real orders while keeping market evaluation and opportunity display active when ordering is off.

**Architecture:** Remove legacy trading-mode gates from runtime and execution, branch only after opportunity publication, and recheck the durable control immediately before venue submission. Present runtime health and real-order permission separately in the API and frontend while retaining automatic incident shutdown and recovery behavior.

**Tech Stack:** Python 3.13, FastAPI, asyncio, pytest, React 19, TypeScript, Vitest, Testing Library, Playwright.

---

### Task 1: Make the durable opening control the sole operator permission

**Files:**
- Modify: `backend/app/api/routes/system_control.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/integration/api/test_kill_switch.py`
- Modify: `backend/tests/unit/test_live_runtime_loop.py`

- [ ] **Step 1: Replace legacy gate tests with direct-control tests**

Delete the tests that expect read-only mode or missing automation evidence to reject enabling. Add:

```python
@pytest.mark.asyncio
async def test_operator_can_enable_opening_without_mode_or_automation_evidence() -> None:
    container = ApplicationContainer()
    container.system_control.opening_enabled = False

    transport = httpx.ASGITransport(app=app_for(container, Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/system-control/opening",
            json={"enabled": True, "reason": "operator enabled real ordering"},
        )

    assert response.status_code == 200
    assert response.json()["opening_enabled"] is True
    assert container.system_control.opening_enabled is True
```

Replace the startup test with one proving startup loads but does not force off a persisted true value.

- [ ] **Step 2: Run the focused control tests and verify they fail**

Run: `uv run pytest backend/tests/integration/api/test_kill_switch.py backend/tests/unit/test_live_runtime_loop.py -v`

Expected: FAIL because the route and startup still enforce trading mode and automation evidence.

- [ ] **Step 3: Remove mode/evidence gates from the route and startup**

Reduce `set_opening_control` to authorization, persistence, and response:

```python
@router.put("/opening")
async def set_opening_control(
    request: OpeningControlRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
    principal: Annotated[Principal, Depends(require_role(Role.OPERATOR))],
) -> dict[str, object]:
    state = await container.system_control.set_opening_async(
        request.enabled,
        request.reason,
        changed_by=principal.subject,
    )
    return {
        "opening_enabled": state.opening_enabled,
        "reason": state.reason,
        "version": state.version,
    }
```

Delete `_apply_startup_gate`, its imports, and its lifespan call. Keep `await application_container.system_control.load_async()` so the durable value remains authoritative.

- [ ] **Step 4: Run the control tests**

Run: `uv run pytest backend/tests/integration/api/test_kill_switch.py backend/tests/unit/test_live_runtime_loop.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the single operator control**

```bash
git add backend/app/api/routes/system_control.py backend/app/main.py backend/tests/integration/api/test_kill_switch.py backend/tests/unit/test_live_runtime_loop.py
git commit -m "feat: make real ordering a direct operator control"
```

### Task 2: Remove trading mode from execution and preserve real incident recovery

**Files:**
- Modify: `backend/app/services/execution.py`
- Modify: `backend/app/services/execution_supervisor.py`
- Modify: `backend/app/services/emergency_hedge.py`
- Modify: `backend/tests/unit/services/test_execution_state_machine.py`
- Modify: `backend/tests/unit/services/test_execution_supervisor.py`
- Modify: `backend/tests/unit/services/test_emergency_hedge.py`
- Modify: `backend/tests/integration/services/test_execution_faults.py`
- Modify: `backend/tests/unit/workers/test_execution_worker.py`

- [ ] **Step 1: Write the final-permission-check test**

Replace the two-layer mode test with a switch-only test and add a control that turns off after authorization consumption:

```python
@pytest.mark.asyncio
async def test_execution_rechecks_opening_before_external_write() -> None:
    ports = {
        Venue.KALSHI: FakeTradingPort(filled(Venue.KALSHI)),
        Venue.POLYMARKET: FakeTradingPort(filled(Venue.POLYMARKET)),
    }
    control = SystemControl(opening_enabled=True)
    saved_states: list[ExecutionState] = []

    class DisablingStore:
        async def save(self, record: ExecutionRecord) -> None:
            saved_states.append(record.state)
            if record.state is ExecutionState.SUBMITTED:
                control.set_opening(False, "operator disabled before venue write")

    service = ControlledExecutionService(ports, control, DisablingStore())
    current_evidence = evidence()
    authorization = ExecutionAuthorizationService().issue(
        MappingStatus.EXACT,
        current_evidence,
        NOW,
    )
    with pytest.raises(AuthorizationRejected, match="real ordering is disabled"):
        await service.execute(
            authorization,
            current_evidence,
            NOW + timedelta(seconds=1),
        )

    assert all(port.submissions == 0 for port in ports.values())
    assert saved_states == [ExecutionState.SUBMITTED, ExecutionState.EXCEPTION]
```

Update supervisor tests to expect `action == "hedge"`, `simulated is False`, and a real emergency request without supplying a mode.

- [ ] **Step 2: Run execution tests and verify signature failures**

Run: `uv run pytest backend/tests/unit/services/test_execution_state_machine.py backend/tests/unit/services/test_execution_supervisor.py backend/tests/unit/services/test_emergency_hedge.py backend/tests/integration/services/test_execution_faults.py -v`

Expected: FAIL because services still require `TradingMode` and simulate non-limited modes.

- [ ] **Step 3: Remove `TradingMode` from execution interfaces**

Change `ExecutionSupervisorPort.finalize` to:

```python
async def finalize(
    self,
    record: ExecutionRecord,
    evidence: ExecutionEvidence | None = None,
    *,
    now: datetime,
    maximum_unhedged_loss: Decimal = Decimal(0),
) -> object | None:
    raise NotImplementedError
```

Change the execution constructor and guard:

```python
class ControlledExecutionService:
    def __init__(
        self,
        ports: Mapping[Venue, ExecutionTradingPort],
        system_control: SystemControl,
        store: ExecutionStore | None = None,
        supervisor: ExecutionSupervisorPort | None = None,
        maximum_unhedged_loss: Decimal = Decimal(0),
    ) -> None:
        self._ports = ports
        self._system_control = system_control
        self._authorizations = ExecutionAuthorizationService()
        self._store = store
        self._supervisor = supervisor
        self._maximum_unhedged_loss = maximum_unhedged_loss

    async def execute(
        self,
        authorization: ExecutionAuthorization,
        current_evidence: ExecutionEvidence,
        now: datetime,
    ) -> ExecutionRecord:
        self._authorizations.consume(authorization, current_evidence, now)
        if not self._system_control.opening_enabled:
            raise AuthorizationRejected("real ordering is disabled")
        requests = self._requests(authorization)
        record = ExecutionRecord(
            correlation_id=authorization.correlation_id,
            state=ExecutionState.PRECHECKED,
            requested_quantity=current_evidence.quantity,
            evidence=current_evidence,
        )
        record.transition(ExecutionState.SUBMITTED, now)
        if self._store is not None:
            await self._store.save(record)

        if not self._system_control.opening_enabled:
            record.transition(ExecutionState.EXCEPTION, now)
            if self._store is not None:
                await self._store.save(record)
            raise AuthorizationRejected("real ordering is disabled")

        results = await asyncio.gather(
            *(self._submit_or_recover(request) for request in requests.values()),
        )
```

The first guard prevents execution creation when ordering was already off. The second guard runs after durable recovery identity persistence and immediately before venue writes; if ordering changed meanwhile, it records an exception and performs no submission. `recover_submitted` keeps no opening check.

- [ ] **Step 4: Make supervisor and emergency hedge always handle real in-flight exposure**

Remove `mode` and simulated branches. Bind with:

```python
def bind_emergency_ports(self, ports: Mapping[Venue, ExecutionTradingPort]) -> None:
    self._emergency_service_factory = lambda: EmergencyHedgeService(dict(ports))
```

Create incidents with `simulated=False`; partial fills use `action="hedge"`; call:

```python
await self._emergency_service_factory().resolve(
    _exposure(record, evidence),
    maximum_unhedged_loss,
)
```

Remove the `trading_mode` constructor argument and simulated early return from `EmergencyHedgeService`.

- [ ] **Step 5: Update all execution call sites and run focused tests**

Remove `TradingMode.LIMITED_AUTO` positional arguments and `mode=` keyword arguments from tests, workers, runtime, and supervisor calls.

Run: `uv run pytest backend/tests/unit/services/test_execution_state_machine.py backend/tests/unit/services/test_execution_supervisor.py backend/tests/unit/services/test_emergency_hedge.py backend/tests/integration/services/test_execution_faults.py backend/tests/unit/workers/test_execution_worker.py -v`

Expected: PASS, including unknown-outcome shutdown, recovery without resubmission, and one real emergency hedge attempt.

- [ ] **Step 6: Commit execution refactoring**

```bash
git add backend/app/services/execution.py backend/app/services/execution_supervisor.py backend/app/services/emergency_hedge.py backend/tests/unit/services/test_execution_state_machine.py backend/tests/unit/services/test_execution_supervisor.py backend/tests/unit/services/test_emergency_hedge.py backend/tests/integration/services/test_execution_faults.py backend/tests/unit/workers/test_execution_worker.py
git commit -m "refactor: authorize execution with real ordering control"
```

### Task 3: Evaluate and publish opportunities while ordering is off

**Files:**
- Modify: `backend/app/services/live_runtime.py`
- Modify: `backend/app/container.py`
- Modify: `backend/tests/integration/services/test_live_runtime.py`

- [ ] **Step 1: Add the off/evaluate/on/execute regression test**

Modify the existing `test_live_cycle_executes_reviewed_profitable_pair_once_per_book_sequence` setup so `opportunities` is a named store passed to `LiveRuntimeService`, initialize `control` as off, and replace the first-cycle assertions with:

```python
@pytest.mark.asyncio
async def test_ordering_off_publishes_opportunity_then_on_executes_next_cycle() -> None:
    off_cycle = await runtime.run_once(NOW)
    [record] = opportunities.list_ranked()

    assert off_cycle == 0
    assert record.rejection_reasons == ()
    assert all(port.submissions == 0 for port in ports.values())
    assert await history.list() == []
    assert capital.reservations == {}

    control.set_opening(True, "operator enabled real ordering")
    on_cycle = await runtime.run_once(NOW + timedelta(milliseconds=100))

    assert on_cycle == 1
    assert all(port.submissions == 1 for port in ports.values())
```

Keep the existing second-cycle and recreated-runtime assertions after this block so duplicate-book and restart idempotency remain covered. Rename the test to `test_ordering_off_publishes_then_on_executes_once_per_book_sequence`.

In the existing stale-book rejection test, set `control = SystemControl(opening_enabled=False)` and retain its rejection assertions. Add `assert all(port.submissions == 0 for port in ports.values())` so both accepted and rejected publication are explicitly covered while ordering is off.

Also assert no capital reservation and no execution record exists after the off cycle.

- [ ] **Step 2: Run the live-runtime tests and verify the empty-opportunity behavior fails**

Run: `uv run pytest backend/tests/integration/services/test_live_runtime.py -v`

Expected: FAIL because `run_once()` returns before market evaluation when opening is off.

- [ ] **Step 3: Move the permission branch after opportunity publication**

Remove `trading_mode` from `LiveRuntimeService.__init__`. Evaluate all pairs first, publish the complete observed list, and only then enter execution handling:

```python
evaluated: list[tuple[ExecutablePair, PairEvaluation]] = []
observed: list[OpportunityRecord] = []
for pair in pairs:
    evaluation = await self._evaluate_pair(pair, policy, market_data, ports, now)
    if evaluation is None:
        continue
    observed.append(evaluation.opportunity)
    evaluated.append((pair, evaluation))

self._opportunities.replace(observed)

for pair, evaluation in evaluated:
```

For each accepted opportunity inside the second loop:

```python
observed.append(evaluation.opportunity)
if evaluation.opportunity.rejection_reasons:
    continue

identity = (pair.id, *evaluation.book_sequences)
if identity in self._processed_books:
    continue

correlation_id = _execution_id(*identity)
executor = ControlledExecutionService(
    ports,
    self._system_control,
    self._execution_store,
    self._execution_supervisor,
    policy.maximum_unhedged_loss,
)
try:
    existing = await self._execution_store.get(correlation_id)
except KeyError:
    existing = None
if existing is not None:
    self._processed_books.add(identity)
    if existing.state is ExecutionState.SUBMITTED:
        await executor.recover_submitted(existing, now)
    continue

if not self._system_control.opening_enabled:
    continue

self._processed_books.add(identity)
reservation = await self._capital_ledger.reserve_pair(
    correlation_id,
    evaluation.kalshi_reserved_amount,
    evaluation.polymarket_reserved_amount,
    event_id=pair.id,
)
```

Because `self._opportunities.replace(observed)` completes before the execution loop, a publication failure prevents all submission work for that cycle. The off branch performs no reservation, authorization/history creation, processed-book mark, or venue submission.

- [ ] **Step 4: Remove the mode argument from container wiring**

Delete `trading_mode=settings.trading_mode` from `ApplicationContainer.runtime()` when constructing `LiveRuntimeService`.

- [ ] **Step 5: Run live-runtime and execution integration tests**

Run: `uv run pytest backend/tests/integration/services/test_live_runtime.py backend/tests/integration/services/test_execution_faults.py -v`

Expected: PASS.

- [ ] **Step 6: Commit continuous evaluation behavior**

```bash
git add backend/app/services/live_runtime.py backend/app/container.py backend/tests/integration/services/test_live_runtime.py
git commit -m "feat: evaluate opportunities while real ordering is off"
```

### Task 4: Separate runtime readiness from real-order permission

**Files:**
- Modify: `backend/app/services/runtime_status.py`
- Modify: `backend/tests/integration/api/test_runtime_status.py`
- Create: `backend/tests/unit/services/test_runtime_status.py`

- [ ] **Step 1: Add readiness tests for both switch states**

Add a focused service test with a fake ready integration service:

```python
from types import SimpleNamespace

import pytest

from backend.app.services.runtime_status import RuntimeStatusService
from backend.app.services.system_control import SystemControl


class ReadyIntegrations:
    async def readiness(self):
        return SimpleNamespace(ready=True, missing=())


@pytest.mark.parametrize("opening_enabled", [False, True])
@pytest.mark.asyncio
async def test_runtime_ready_does_not_depend_on_real_order_permission(
    opening_enabled: bool,
) -> None:
    control = SystemControl(opening_enabled=opening_enabled)
    service = RuntimeStatusService(ReadyIntegrations(), control)
    service.running = True

    response = await service.view()

    assert response.ready is True
    assert response.running is True
    assert response.opening_enabled is opening_enabled
```

- [ ] **Step 2: Run runtime and health tests and verify legacy expectations fail**

Run: `uv run pytest backend/tests/unit/services/test_runtime_status.py backend/tests/integration/api/test_runtime_status.py -v`

Expected: FAIL until readiness tests stop treating the real-order switch as a runtime prerequisite.

- [ ] **Step 3: Keep readiness independent from order permission**

Keep `RuntimeStatusService.view()` readiness based on integration readiness only. `record_cycle(error=None)` clears the previous cycle error; the runtime no longer records an error merely because ordering is off. Keep the temporary legacy `/health.trading_mode` field until Task 6 so backend and frontend remain compatible at every commit.

- [ ] **Step 4: Run runtime status tests**

Run: `uv run pytest backend/tests/unit/services/test_runtime_status.py backend/tests/integration/api/test_runtime_status.py -v`

Expected: PASS.

- [ ] **Step 5: Commit status separation**

```bash
git add backend/app/services/runtime_status.py backend/tests/unit/services/test_runtime_status.py backend/tests/integration/api/test_runtime_status.py
git commit -m "feat: separate evaluation health from order permission"
```

### Task 5: Add the single Real Ordering toggle to Integrations

**Files:**
- Modify: `frontend/src/types/system.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/App.tsx`
- Create: `frontend/src/components/RealOrderingBanner.tsx`
- Delete: `frontend/src/components/TradingModeBanner.tsx`
- Modify: `frontend/src/components/RuntimeStatusBand.tsx`
- Modify: `frontend/src/pages/IntegrationSettingsPage.tsx`
- Modify: `frontend/src/App.css`
- Modify: `frontend/src/App.test.tsx`

- [ ] **Step 1: Write failing UI toggle and status tests**

Add tests for both successful and failed PUTs:

```tsx
test('toggles real ordering from the integrations page', async () => {
  let requestBody: Record<string, unknown> | null = null
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/system-control/opening')) {
      requestBody = JSON.parse(String(init?.body)) as Record<string, unknown>
      return Promise.resolve({
        ok: true,
        json: async () => ({
          opening_enabled: true,
          reason: String(requestBody?.reason),
          version: 2,
        }),
      })
    }
    if (url.includes('/api/runtime')) {
      return Promise.resolve({ ok: true, json: async () => runtimeStatus })
    }
    if (url.includes('/health')) {
      return Promise.resolve({
        ok: true,
        json: async () => ({
          status: 'ok',
          opening_enabled: false,
          reason: 'safe default',
        }),
      })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  }))

  render(<App />)
  expect(await screen.findByText('行情评估运行中')).toBeInTheDocument()
  expect(screen.getByText('真实下单已关闭')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '集成' }))
  fireEvent.click(await screen.findByRole('switch', { name: '真实下单' }))

  await waitFor(() => expect(requestBody).toEqual({
    enabled: true,
    reason: 'operator enabled real ordering',
  }))
  expect(await screen.findByText('真实下单已开启')).toBeInTheDocument()
})

test('failed real-order toggle keeps the previous state', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/system-control/opening')) {
      return Promise.resolve({
        ok: false,
        status: 503,
        json: async () => ({ detail: 'control store unavailable' }),
      })
    }
    if (url.includes('/api/runtime')) {
      return Promise.resolve({ ok: true, json: async () => runtimeStatus })
    }
    if (url.includes('/health')) {
      return Promise.resolve({
        ok: true,
        json: async () => ({
          status: 'ok',
          opening_enabled: false,
          reason: 'safe default',
        }),
      })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  }))
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '集成' }))
  fireEvent.click(await screen.findByRole('switch', { name: '真实下单' }))

  expect(await screen.findByText('control store unavailable')).toBeInTheDocument()
  expect(screen.getByRole('switch', { name: '真实下单' })).not.toBeChecked()
})
```

- [ ] **Step 2: Run frontend tests and verify they fail**

Run: `npm --prefix frontend test -- frontend/src/App.test.tsx`

Expected: FAIL because there is no switch or control API helper.

- [ ] **Step 3: Simplify system types and add the control request**

Replace `frontend/src/types/system.ts` with:

```ts
export interface SystemStatus {
  status: string
  opening_enabled: boolean
  reason: string
}

export interface OpeningControlState {
  opening_enabled: boolean
  reason: string
  version: number
}
```

Add:

```ts
export function setRealOrdering(enabled: boolean): Promise<OpeningControlState> {
  return apiRequest<OpeningControlState>('/api/system-control/opening', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      enabled,
      reason: enabled
        ? 'operator enabled real ordering'
        : 'operator disabled real ordering',
    }),
  })
}
```

- [ ] **Step 4: Lift switch state into App and pass it to Integrations**

Initialize `systemStatus` with `opening_enabled: false`. Add:

```tsx
const updateRealOrdering = async (enabled: boolean) => {
  const state = await setRealOrdering(enabled)
  setSystemStatus((current) => ({
    ...current,
    opening_enabled: state.opening_enabled,
    reason: state.reason,
  }))
}
```

Render:

```tsx
<RealOrderingBanner openingEnabled={systemStatus.opening_enabled} />
<RuntimeStatusBand status={runtimeStatus} />
{view === 'integrations' && (
  <IntegrationSettingsPage
    realOrderingEnabled={systemStatus.opening_enabled}
    onRealOrderingChange={updateRealOrdering}
  />
)}
```

- [ ] **Step 5: Replace mode presentation with one permission status**

Create `RealOrderingBanner.tsx`, update the App import, and remove `TradingModeBanner.tsx`:

```tsx
export function RealOrderingBanner({ openingEnabled }: { openingEnabled: boolean }) {
  return (
    <div className="mode-band">
      <div className="mode-primary">
        {openingEnabled ? <Power size={16} /> : <LockKeyhole size={16} />}
        <strong>{openingEnabled ? '真实下单已开启' : '真实下单已关闭'}</strong>
        <span>{openingEnabled ? '符合风控条件的机会可以提交订单' : '仅评估和展示机会，不提交新订单'}</span>
      </div>
    </div>
  )
}
```

Change `RuntimeStatusBand` active logic to `status.ready && status.running && !status.last_error`. Its healthy title is `行情评估运行中`; failure title is `运行未就绪`. Do not treat `opening_enabled === false` as a reason or blocked state.

- [ ] **Step 6: Add the Integrations execution panel**

Accept the two props, keep local pending/error state, and insert this panel above provider cards:

```tsx
<section className="real-ordering-panel" aria-labelledby="real-ordering-title">
  <div>
    <h3 id="real-ordering-title">交易执行</h3>
    <p>
      {realOrderingEnabled
        ? '已开启：符合全部安全条件的机会可以提交真实订单。'
        : '已关闭：行情、审核和机会评估继续运行，不会提交新订单。'}
    </p>
    {orderingError && <span className="operation-message">{orderingError}</span>}
  </div>
  <label className="real-ordering-switch">
    <span>真实下单</span>
    <input
      type="checkbox"
      role="switch"
      checked={realOrderingEnabled}
      disabled={orderingPending}
      onChange={(event) => void changeRealOrdering(event.target.checked)}
    />
  </label>
</section>
```

`changeRealOrdering` awaits the parent callback, sets no optimistic state, and reports the thrown `Error.message`. Add responsive CSS for the panel and switch without adding another confirmation dialog.

- [ ] **Step 7: Run frontend unit tests and build**

Run: `npm --prefix frontend test -- frontend/src/App.test.tsx`

Expected: PASS.

Run: `npm --prefix frontend run build`

Expected: PASS.

- [ ] **Step 8: Commit the single-toggle UI**

```bash
git add frontend/src/types/system.ts frontend/src/api/client.ts frontend/src/App.tsx frontend/src/components/RealOrderingBanner.tsx frontend/src/components/TradingModeBanner.tsx frontend/src/components/RuntimeStatusBand.tsx frontend/src/pages/IntegrationSettingsPage.tsx frontend/src/App.css frontend/src/App.test.tsx
git commit -m "feat: add real ordering toggle to integrations"
```

### Task 6: Remove legacy mode configuration and update browser coverage

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/integration/api/test_integrations.py`
- Modify: `backend/tests/unit/test_health.py`
- Modify: `frontend/tests/runtime.spec.ts`
- Modify: `frontend/tests/opportunities.spec.ts`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docs/runbooks/kill-switch.md`
- Create: `docs/acceptance/real-ordering.md`
- Delete: `docs/acceptance/read-only.md`
- Delete: `docs/acceptance/shadow.md`
- Delete: `docs/acceptance/limited-auto.md`
- Delete: `docs/acceptance/canary-auto.md`

- [ ] **Step 1: Prove no runtime code depends on legacy mode**

Run: `rg -n "TradingMode|trading_mode|TRADING_MODE" backend/app frontend/src`

Expected before cleanup: matches remain in `core/config.py` and any missed call sites. Expected after cleanup: no matches in active backend/frontend code.

- [ ] **Step 2: Remove the enum and setting**

Delete `TradingMode` and `Settings.trading_mode`. Keep:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    opening_enabled: bool = False
    database_url: str = "postgresql+asyncpg://poly:poly@localhost:5432/poly"
    credential_service_name: str = "poly-controlled-execution"
    local_setup_enabled: bool = False
    runtime_poll_seconds: float = 5.0
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
```

Rename `test_explicit_local_setup_allows_loopback_in_real_trading_mode` to `test_explicit_local_setup_allows_trusted_loopback` and remove its mode monkeypatch; local access security remains unchanged.

Remove the mode field from `/health`:

```python
return {
    "status": "ok",
    "opening_enabled": application_container.system_control.opening_enabled,
    "reason": application_container.system_control.reason,
}
```

Update `test_health.py` to assert the default setting is only `opening_enabled is False` and that the health response contains no `trading_mode` key.

- [ ] **Step 3: Update fixtures and operator documentation**

Remove `trading_mode` from `/health` fixtures. Update browser assertions to expect `行情评估运行中` and either `真实下单已开启` or `真实下单已关闭`. Remove `TRADING_MODE` from `.env.example`, README startup instructions, and the kill-switch recovery runbook. Replace the four mode-specific acceptance documents with `docs/acceptance/real-ordering.md`, containing these checks:

```markdown
# Real ordering acceptance

1. Start with the durable Real ordering switch off.
2. Confirm discovery, review, native market reads, and accepted/rejected Opportunities continue.
3. Confirm no capital reservation, execution record, or venue submission is created while off.
4. Enable Real ordering from Integrations and confirm one currently eligible opportunity can submit.
5. Disable Real ordering and confirm subsequent new openings stop while reconciliation continues.
6. Trigger the incident fixture and confirm the switch is automatically disabled.
```

- [ ] **Step 4: Run browser-facing tests**

Run: `npm --prefix frontend test`

Expected: PASS.

Run: `npm --prefix frontend exec -- playwright test frontend/tests/runtime.spec.ts frontend/tests/opportunities.spec.ts`

Expected: PASS at desktop and mobile viewports.

- [ ] **Step 5: Run legacy-reference scan**

Run: `rg -n "TradingMode|trading_mode|TRADING_MODE|READ ONLY|SHADOW|LIMITED AUTO" backend/app frontend/src .env.example README.md docs/runbooks`

Expected: no matches.

- [ ] **Step 6: Commit legacy cleanup**

```bash
git add backend/app/core/config.py backend/app/main.py backend/tests/integration/api/test_integrations.py backend/tests/unit/test_health.py frontend/tests/runtime.spec.ts frontend/tests/opportunities.spec.ts .env.example README.md docs/runbooks/kill-switch.md docs/acceptance/real-ordering.md docs/acceptance/read-only.md docs/acceptance/shadow.md docs/acceptance/limited-auto.md docs/acceptance/canary-auto.md
git commit -m "refactor: remove legacy trading modes"
```

### Task 7: Run full safety and usability verification

**Files:**
- Modify only if a verified regression requires it: files already listed above

- [ ] **Step 1: Run backend static checks**

Run: `uv run ruff check backend/app backend/tests migrations`

Expected: PASS.

Run: `uv run mypy backend/app`

Expected: PASS.

- [ ] **Step 2: Run backend tests**

Run: `uv run pytest backend/tests/unit backend/tests/integration/api backend/tests/integration/services -q`

Expected: PASS, including off-state opportunity publication, on-state execution, switch recheck, incident auto-disable, unknown-outcome recovery, fee mismatch shutdown, and emergency hedge behavior.

- [ ] **Step 3: Run frontend checks**

Run: `npm --prefix frontend run lint`

Expected: PASS.

Run: `npm --prefix frontend test`

Expected: PASS.

Run: `npm --prefix frontend run build`

Expected: PASS.

- [ ] **Step 4: Run responsive browser tests**

Run: `npm --prefix frontend exec -- playwright test`

Expected: PASS with no horizontal overflow and the Integrations toggle usable at desktop and mobile sizes.

- [ ] **Step 5: Perform a final source and diff audit**

Run: `rg -n "TradingMode|trading_mode|TRADING_MODE|automatic execution not ready|开仓总开关" backend/app frontend/src .env.example README.md docs/runbooks`

Expected: no legacy control text or mode dependency remains.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 6: Confirm the implementation worktree is clean**

Run: `git status --short`

Expected: no output. If a verification command required a correction, return to the task that owns that file, repeat its focused tests, use that task's explicit commit command, and then rerun this final audit.
