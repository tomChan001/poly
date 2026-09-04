# Unavailable Opportunity Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Represent uncalculated opportunity metrics as null and render them as em dashes with the rejection reason visible in the table.

**Architecture:** Make metric availability explicit in `OpportunityRecord`, while retaining book or quote evidence actually calculated before rejection. Keep the API as direct dataclass serialization and make the React page format nullable fields independently.

**Tech Stack:** Python 3.12, FastAPI, pytest, TypeScript 6, React 19, Vitest, Testing Library, Vite.

---

### Task 1: Make unavailable metrics explicit in the opportunity contract

**Files:**
- Modify: `backend/app/services/opportunities.py:7-63`
- Test: `backend/tests/integration/api/test_opportunities.py`

- [ ] **Step 1: Write the failing API and ranking test**

Import `replace` and make the rejected fixture genuinely unavailable:

```python
from dataclasses import replace

rejected = replace(
    OpportunityRecord.example("rejected", Decimal(0), ("STALE_BOOK",)),
    quantity=None,
    kalshi_vwap=None,
    polymarket_vwap=None,
    total_fees=None,
    deployed_capital=None,
    payout=None,
    profit_floor=None,
    conservative_roi=None,
    book_age_ms=None,
)
```

Use `rejected` in the existing store input and add:

```python
assert body[-1]["quantity"] is None
assert body[-1]["kalshi_vwap"] is None
assert body[-1]["polymarket_vwap"] is None
assert body[-1]["deployed_capital"] is None
assert body[-1]["profit_floor"] is None
assert body[-1]["conservative_roi"] is None
assert body[-1]["book_age_ms"] is None
```

- [ ] **Step 2: Run the focused test and verify the contract fails at ranking**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/integration/api/test_opportunities.py::test_opportunities_are_sorted_by_roi_and_explain_rejections -q
```

Expected: FAIL because `list_ranked()` compares `None` with `Decimal`.

- [ ] **Step 3: Make metric fields nullable and rank unavailable rows last**

Change the `OpportunityRecord` metric annotations:

```python
quantity: Decimal | None
kalshi_vwap: Decimal | None
polymarket_vwap: Decimal | None
total_fees: Decimal | None
deployed_capital: Decimal | None
payout: Decimal | None
profit_floor: Decimal | None
conservative_roi: Decimal | None
# date fields stay non-null
book_age_ms: int | None
```

Replace the ranking key:

```python
key=lambda item: (
    item.conservative_roi is not None,
    item.conservative_roi or Decimal(0),
    item.profit_floor is not None,
    item.profit_floor or Decimal(0),
),
```

With `reverse=True`, a genuine zero ranks ahead of an unavailable value.

- [ ] **Step 4: Run the focused test and verify it passes**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Commit the contract change**

```powershell
git add backend/app/services/opportunities.py backend/tests/integration/api/test_opportunities.py
git commit -m "fix: distinguish unavailable opportunity metrics"
```

### Task 2: Preserve only evidence reached before runtime rejection

**Files:**
- Modify: `backend/app/services/live_runtime.py:30-31,347-484,539-610`
- Test: `backend/tests/integration/services/test_live_runtime.py:1113-1240`

- [ ] **Step 1: Add failing assertions for early and partial evaluation**

Extend `test_late_settlement_is_retained_as_structured_rejection`:

```python
assert record.quantity is None
assert record.kalshi_vwap is None
assert record.polymarket_vwap is None
assert record.total_fees is None
assert record.deployed_capital is None
assert record.payout is None
assert record.profit_floor is None
assert record.conservative_roi is None
assert record.book_age_ms is None
```

Extend `test_stale_book_is_retained_as_structured_rejection`:

```python
assert record.quantity is None
assert record.kalshi_vwap is None
assert record.polymarket_vwap is None
assert record.deployed_capital is None
assert record.profit_floor is None
assert record.conservative_roi is None
assert record.book_age_ms == 5000
```

- [ ] **Step 2: Add a failing post-quote rejection test**

Add `from dataclasses import replace` at the top of the test file, then add:

```python
@pytest.mark.asyncio
async def test_below_minimum_quantity_retains_calculated_quote_metrics() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Below minimum pair",
            kalshi_market_id="K-BELOW-MINIMUM",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-BELOW-MINIMUM",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(21),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    risk_input = replace(
        RiskPolicyInput.defaults(),
        per_trade_limit=Decimal(100),
        per_event_limit=Decimal(100),
        portfolio_limit=Decimal(100),
    )
    risk = InMemoryRiskPolicyStore(risk_input)
    control = SystemControl(opening_enabled=False)
    history = InMemoryExecutionStore()
    opportunities = InMemoryOpportunityStore()
    status = RuntimeStatusService(integrations, control)
    ports = {
        Venue.KALSHI: FakeTradingPort(Venue.KALSHI),
        Venue.POLYMARKET: FakeTradingPort(Venue.POLYMARKET),
    }
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=risk,
        system_control=control,
        execution_store=history,
        opportunities=opportunities,
        runtime_status=status,
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=CapitalLedger({}),
    )

    executions = await runtime.run_once(NOW)
    [record] = opportunities.list_ranked()
    assert executions == 0
    assert record.rejection_reasons == ("BELOW_MINIMUM_QUANTITY",)
    assert record.quantity == Decimal(20)
    assert record.kalshi_vwap == Decimal("0.70")
    assert record.polymarket_vwap == Decimal("0.20")
    assert record.deployed_capital is not None
    assert record.profit_floor is not None
    assert record.conservative_roi is not None
    assert record.book_age_ms == 0
    assert await history.list() == []
    assert all(port.submissions == 0 for port in ports.values())
```

- [ ] **Step 3: Run the selected runtime tests and verify they fail**

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/integration/services/test_live_runtime.py -k "late_settlement or stale_book or below_minimum_quantity" -q
```

Expected: FAIL because the rejection builder still emits sentinel zeroes and
discards the below-minimum quote.

- [ ] **Step 4: Accept an optional quote in the rejection builder**

Import `ExecutableQuote` and extend the signature:

```python
from backend.app.services.optimizer import (
    ExecutableQuote,
    QuoteOptimizer,
    QuotePolicy,
)

def _rejected_evaluation(
    pair: ExecutablePair,
    policy: RiskPolicy,
    now: datetime,
    reasons: tuple[str, ...],
    *,
    kalshi_book: NormalizedBook | None = None,
    polymarket_book: NormalizedBook | None = None,
    balance_versions: tuple[str, str] = ("unavailable", "unavailable"),
    quote: ExecutableQuote | None = None,
) -> PairEvaluation:
```

Calculate record fields from the evidence:

```python
books = tuple(book for book in (kalshi_book, polymarket_book) if book is not None)
book_ages = [int((now - book.received_at).total_seconds() * 1000) for book in books]
age_ms = max(book_ages) if book_ages else None
quantity = quote.quantity if quote is not None else None
kalshi_vwap = quote.kalshi_cost / quote.quantity if quote is not None else None
polymarket_vwap = quote.polymarket_cost / quote.quantity if quote is not None else None
total_fees = quote.kalshi_fee + quote.polymarket_fee if quote is not None else None
deployed_capital = quote.deployed_capital if quote is not None else None
payout = quote.quantity if quote is not None else None
profit_floor = quote.profit_floor if quote is not None else None
conservative_roi = quote.conservative_roi if quote is not None else None
```

Assign those names to `OpportunityRecord`. Keep the internal rejected
`PairEvaluation` execution values at zero because rejected evaluations are
skipped before reservation and submission.

- [ ] **Step 5: Pass the quote on the below-minimum path**

```python
return _rejected_evaluation(
    pair,
    policy,
    now,
    ("BELOW_MINIMUM_QUANTITY",),
    kalshi_book=books.kalshi,
    polymarket_book=books.polymarket,
    balance_versions=(str(kalshi_balance), str(polymarket_balance)),
    quote=quote,
)
```

- [ ] **Step 6: Run the selected runtime tests and verify they pass**

Run the command from Step 3. Expected: all three selected tests PASS.

- [ ] **Step 7: Commit the runtime evidence change**

```powershell
git add backend/app/services/live_runtime.py backend/tests/integration/services/test_live_runtime.py
git commit -m "fix: retain available rejection evidence"
```

### Task 3: Render unavailable metrics and the rejection reason clearly

**Files:**
- Modify: `frontend/src/types/opportunity.ts`
- Modify: `frontend/src/pages/OpportunitiesPage.tsx`
- Create: `frontend/src/pages/OpportunitiesPage.test.tsx`

- [ ] **Step 1: Write the failing null-display test**

Create the file with the complete fixture and first test:

```tsx
import '@testing-library/jest-dom/vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'

import type { Opportunity } from '../types/opportunity'
import { OpportunitiesPage } from './OpportunitiesPage'

const lateOpportunity: Opportunity = {
  id: 'late',
  event: 'Late settlement pair',
  kalshi_outcome: 'no',
  polymarket_outcome: 'yes',
  mapping_status: 'exact',
  quantity: null,
  kalshi_vwap: null,
  polymarket_vwap: null,
  total_fees: null,
  deployed_capital: null,
  payout: null,
  profit_floor: null,
  conservative_roi: null,
  expected_settlement_at: '2027-01-25T15:00:00Z',
  worst_case_settlement_at: '2027-01-25T15:00:00Z',
  book_age_ms: null,
  rejection_reasons: ['SETTLEMENT_TOO_LATE'],
  fee_status: 'unknown',
}

test('shows unavailable rejection metrics as dashes with the reason in the row', () => {
  render(
    <OpportunitiesPage
      opportunities={[lateOpportunity]}
      activeCount={0}
      loading={false}
    />,
  )

  const row = screen.getByText('Late settlement pair').closest('tr')
  expect(row).not.toBeNull()
  expect(within(row!).getByText('已拒绝 · 最晚结算时间超出限制')).toBeInTheDocument()
  expect(within(row!).getByText('K no @—')).toBeInTheDocument()
  expect(within(row!).getByText('P yes @—')).toBeInTheDocument()
  expect(within(row!).getAllByText('—')).toHaveLength(5)
  fireEvent.click(within(row!).getByText('Late settlement pair'))
  const drawer = screen.getByRole('complementary', { name: '机会详情' })
  expect(within(drawer).getAllByText('—').length).toBeGreaterThan(0)
  expect(within(drawer).getByText('最晚结算时间超出限制')).toBeInTheDocument()
  expect(within(screen.getByLabelText('机会概览')).getByText('—')).toBeInTheDocument()
})
```

- [ ] **Step 2: Write tests for partial and normal metrics**

Add both tests to the same file:

```tsx
test('keeps measured book age on a rejection without a quote', () => {
  const staleOpportunity: Opportunity = {
    ...lateOpportunity,
    id: 'stale',
    event: 'Stale book pair',
    book_age_ms: 2501,
    rejection_reasons: ['STALE_BOOK'],
  }
  render(
    <OpportunitiesPage
      opportunities={[staleOpportunity]}
      activeCount={0}
      loading={false}
    />,
  )

  const row = screen.getByText('Stale book pair').closest('tr')
  expect(row).not.toBeNull()
  expect(within(row!).getByText('2501 ms')).toHaveClass('age', 'stale')
})

test('keeps existing formatting for a calculated opportunity', () => {
  const calculated: Opportunity = {
    ...lateOpportunity,
    id: 'calculated',
    event: 'Calculated pair',
    quantity: '10',
    kalshi_vwap: '0.70',
    polymarket_vwap: '0.20',
    total_fees: '0.02',
    deployed_capital: '9.02',
    payout: '10',
    profit_floor: '0.98',
    conservative_roi: '0.1086',
    book_age_ms: 180,
    rejection_reasons: [],
    fee_status: 'calculated',
  }
  render(
    <OpportunitiesPage
      opportunities={[calculated]}
      activeCount={1}
      loading={false}
    />,
  )

  const row = screen.getByText('Calculated pair').closest('tr')
  expect(row).not.toBeNull()
  expect(within(row!).getByText('K no @0.70')).toBeInTheDocument()
  expect(within(row!).getByText('P yes @0.20')).toBeInTheDocument()
  expect(within(row!).getByText('10')).toBeInTheDocument()
  expect(within(row!).getByText('$9.02')).toBeInTheDocument()
  expect(within(row!).getByText('$0.98')).toBeInTheDocument()
  expect(within(row!).getByText('10.86%')).toBeInTheDocument()
  expect(within(row!).getByText('180 ms')).toBeInTheDocument()
})
```

- [ ] **Step 3: Run the component test and verify failure**

```powershell
npm --prefix frontend test -- src/pages/OpportunitiesPage.test.tsx
```

Expected: FAIL because the page converts null to numeric zero and omits the row reason.

- [ ] **Step 4: Update the frontend type**

```ts
quantity: string | null
kalshi_vwap: string | null
polymarket_vwap: string | null
total_fees: string | null
deployed_capital: string | null
payout: string | null
profit_floor: string | null
conservative_roi: string | null
book_age_ms: number | null
```

- [ ] **Step 5: Add nullable formatters and summaries**

```tsx
const numberValue = (value: string | null | undefined) =>
  value == null ? null : Number(value)
const money = (value: string | null | undefined) => {
  const parsed = numberValue(value)
  return parsed == null ? '—' : `$${parsed.toFixed(2)}`
}
const percent = (value: string | null | undefined) => {
  const parsed = numberValue(value)
  return parsed == null ? '—' : `${(parsed * 100).toFixed(2)}%`
}
const quantity = (value: string | null | undefined) => {
  const parsed = numberValue(value)
  return parsed == null ? '—' : parsed.toFixed(0)
}
const age = (value: number | null | undefined) =>
  value == null ? '—' : `${value} ms`
```

Build `calculatedRois` by filtering null values, make `bestRoi` null when that
array is empty, and render `—` for a null best ROI. Sum capital with
`numberValue(item.deployed_capital) ?? 0`. Use the helpers in the table and
drawer, and apply stale styling only when age is present and above 2000.

- [ ] **Step 6: Show the first localized rejection reason in the row**

```tsx
{rejected
  ? `已拒绝 · ${rejectionLabel(item.rejection_reasons[0])}`
  : item.mapping_status.toUpperCase()}
```

- [ ] **Step 7: Run the component tests and verify they pass**

Run the command from Step 3. Expected: all component tests PASS.

- [ ] **Step 8: Commit the frontend behavior**

```powershell
git add frontend/src/types/opportunity.ts frontend/src/pages/OpportunitiesPage.tsx frontend/src/pages/OpportunitiesPage.test.tsx
git commit -m "fix: label unavailable opportunity metrics"
```

### Task 4: Verify the complete change

**Files:**
- Verify only; do not modify unrelated dirty files.

- [ ] **Step 1: Run backend opportunity and runtime tests**

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/integration/api/test_opportunities.py backend/tests/integration/services/test_live_runtime.py -q
```

Expected: all selected backend tests PASS.

- [ ] **Step 2: Run the complete frontend unit suite**

```powershell
npm --prefix frontend test
```

Expected: all frontend unit tests PASS.

- [ ] **Step 3: Run frontend lint and production build**

```powershell
npm --prefix frontend run lint
npm --prefix frontend run build
```

Expected: both commands exit 0 with no lint or TypeScript errors.

- [ ] **Step 4: Inspect the final diff and working tree**

```powershell
git diff --check HEAD~3..HEAD
git status --short
```

Expected: no whitespace errors; only pre-existing unrelated working-tree
changes remain unstaged.
