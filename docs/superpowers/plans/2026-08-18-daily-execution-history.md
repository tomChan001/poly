# Daily Execution History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated web menu that groups execution statistics and order history by Beijing calendar day.

**Architecture:** Keep `/api/executions` as the single source of truth. A focused frontend history module derives each execution's day from its earliest state transition, groups records in descending date order, and the page renders one selected day's totals and details.

**Tech Stack:** React 19, TypeScript, Vitest, Testing Library, Playwright, existing CSS system and Lucide icons.

---

### Task 1: Lock the daily-history behavior with a failing test

**Files:**
- Modify: `frontend/src/App.test.tsx`

- [ ] **Step 1: Add records on two Beijing dates and open the new menu**

```tsx
fireEvent.click(await screen.findByRole('button', { name: '历史' }))
expect(screen.getByRole('heading', { name: '每日执行历史' })).toBeInTheDocument()
expect(screen.getByLabelText('选择日期')).toHaveValue('2026-08-18')
```

- [ ] **Step 2: Assert that changing the day changes both totals and records**

```tsx
fireEvent.change(screen.getByLabelText('选择日期'), { target: { value: '2026-08-17' } })
expect(screen.getByText('corr-previous')).toBeInTheDocument()
expect(screen.queryByText('corr-latest')).not.toBeInTheDocument()
```

- [ ] **Step 3: Run the focused test and verify RED**

Run: `cd frontend; npm test -- --run src/App.test.tsx`

Expected: FAIL because the `历史` navigation button does not exist.

### Task 2: Add deterministic Beijing-day grouping

**Files:**
- Create: `frontend/src/utils/executionHistory.ts`

- [ ] **Step 1: Derive the first execution timestamp**

```ts
export function executionTimestamp(execution: Execution): Date | null {
  const timestamps = execution.transitions
    .map((transition) => new Date(transition.occurred_at))
    .filter((value) => !Number.isNaN(value.getTime()))
  return timestamps.length ? new Date(Math.min(...timestamps.map((value) => value.getTime()))) : null
}
```

- [ ] **Step 2: Group dated records and calculate daily totals**

Each bucket exposes `date`, `executions`, `total`, `paired`, `matchedQuantity`, and `unhedgedQuantity`. Use `Intl.DateTimeFormat` with `timeZone: 'Asia/Shanghai'`; do not group by the browser's local timezone.

- [ ] **Step 3: Keep undated legacy records visible**

Place records without a valid transition timestamp in an `undated` bucket labeled `日期未知`, after all dated buckets.

### Task 3: Build the history page and navigation entry

**Files:**
- Create: `frontend/src/pages/HistoryPage.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.css`

- [ ] **Step 1: Add `history` to the `View` union and navigation**

```tsx
{ id: 'history' as const, label: '历史', icon: History }
```

- [ ] **Step 2: Render the selected day**

The page contains a date selector, four fixed summary metrics, and a compact execution table. Default selection is the newest available day and reset it only when the selected day disappears from refreshed data.

- [ ] **Step 3: Render complete empty states**

When there are no executions, show `暂无执行历史`; when a selected bucket has no rows, show `当日无执行记录`.

- [ ] **Step 4: Make six navigation items and the history table fit mobile**

Use a six-column bottom navigation at `max-width: 680px`; allow the history table to scroll horizontally without widening the document.

### Task 4: Verify behavior and layout

**Files:**
- Modify: `frontend/tests/opportunities.spec.ts`

- [ ] **Step 1: Add a browser assertion for the history menu**

Open `历史`, assert the latest date, daily totals, and a visible execution ID at desktop and mobile sizes.

- [ ] **Step 2: Run all frontend verification**

Run: `npm test`, `npm run lint`, `npm run build`, and `npx playwright test` from `frontend`.

Expected: all commands exit 0 and the desktop/mobile geometry checks report no document overflow or overlapping controls.

### Self-Review

- The new menu is independent from the existing runtime analytics page.
- Dates are explicitly Beijing dates, so deployment timezone cannot change grouping.
- Existing execution records remain the only data source; no duplicate counter storage is introduced.
- Undated legacy records are retained rather than silently dropped.
- Git steps are intentionally omitted because the user explicitly requested no Git handling.
