# Pair Review Draft Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep unsaved review checklist and note edits intact while the candidate list refreshes every five seconds.

**Architecture:** Keep candidate records synchronized as they are today, but treat the selected review form as a separate local draft. Track the candidate that owns the draft and whether the user has edited it; only initialize from server data when selecting a different candidate or after a successful save.

**Tech Stack:** React 19, TypeScript, Vitest, Testing Library

---

## File Structure

- Modify `frontend/src/App.test.tsx`: add a regression test that advances the candidate refresh interval and verifies unsaved form state survives.
- Modify `frontend/src/pages/PairSettingsPage.tsx`: add selected-candidate draft ownership and dirty-state protection.

### Task 1: Reproduce the refresh overwrite

**Files:**
- Modify: `frontend/src/App.test.tsx`
- Test: `frontend/src/App.test.tsx`

- [ ] **Step 1: Import `act` for timer-driven React updates**

Change the Testing Library import to:

```tsx
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
```

- [ ] **Step 2: Add the failing regression test**

Add this test after the existing market-equivalence review test so it can reuse `pendingPair`, `runtimeStatus`, and `opportunities`:

```tsx
test('preserves an unsaved review draft across automatic candidate refreshes', async () => {
  vi.useFakeTimers()
  try {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/api/runtime')) {
          return Promise.resolve({ ok: true, json: async () => runtimeStatus })
        }
        if (url.endsWith('/api/pairs')) {
          return Promise.resolve({ ok: true, json: async () => [pendingPair] })
        }
        if (url.includes('/health')) {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              status: 'ok',
              trading_mode: 'limited_auto',
              opening_enabled: true,
              reason: 'configured default',
            }),
          })
        }
        return Promise.resolve({
          ok: true,
          json: async () => (url.includes('/api/executions') ? [] : opportunities),
        })
      }),
    )

    render(<App />)
    await act(async () => { await Promise.resolve() })
    fireEvent.click(screen.getByRole('button', { name: '审核' }))
    await act(async () => { await Promise.resolve() })

    const subject = screen.getByRole('checkbox', { name: '已核对标的主体' })
    const notes = screen.getByRole('textbox', { name: '审核备注' })
    fireEvent.click(subject)
    fireEvent.change(notes, { target: { value: '尚未提交的审核备注' } })

    await act(async () => {
      vi.advanceTimersByTime(5_000)
      await Promise.resolve()
    })

    expect(subject).toBeChecked()
    expect(notes).toHaveValue('尚未提交的审核备注')
  } finally {
    vi.useRealTimers()
  }
})
```

- [ ] **Step 3: Run the test and verify the expected failure**

Run:

```powershell
npm test -- --run --testNamePattern="preserves an unsaved review draft"
```

Expected: FAIL because the checkbox becomes unchecked or the note becomes empty after the five-second refresh.

- [ ] **Step 4: Add switching-candidate behavior coverage**

Add a characterization test for the already-supported switch behavior. It should return two candidates, edit the first candidate, select the second candidate, and verify that the second candidate's saved checklist and notes load immediately. Stub `window.confirm` with `vi.fn()` and assert that it is not called. This test is expected to pass before the production fix because switching without a prompt is existing behavior that the dirty-state guard must preserve.

```tsx
test('discards the current draft without prompting when switching candidates', async () => {
  const confirm = vi.fn()
  const secondPair = {
    ...pendingPair,
    id: 'pair-2',
    title: '第二个互补市场',
    checklist: { subject: true },
    notes: '第二个候选的已保存备注',
  }
  vi.stubGlobal('confirm', confirm)
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      if (url.endsWith('/api/pairs')) {
        return Promise.resolve({ ok: true, json: async () => [pendingPair, secondPair] })
      }
      if (url.includes('/health')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            status: 'ok',
            trading_mode: 'limited_auto',
            opening_enabled: true,
            reason: 'configured default',
          }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: async () => (url.includes('/api/executions') ? [] : opportunities),
      })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '审核' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: '已核对标的主体' }))
  fireEvent.change(screen.getByRole('textbox', { name: '审核备注' }), {
    target: { value: '第一个候选的未保存备注' },
  })
  fireEvent.click(screen.getByRole('button', { name: /第二个互补市场/ }))

  expect(screen.getByRole('checkbox', { name: '已核对标的主体' })).toBeChecked()
  expect(screen.getByRole('textbox', { name: '审核备注' })).toHaveValue('第二个候选的已保存备注')
  expect(confirm).not.toHaveBeenCalled()
})
```

- [ ] **Step 5: Run the switching-candidate characterization test**

Run:

```powershell
npm test -- --run --testNamePattern="discards the current draft without prompting"
```

Expected: PASS before and after the fix.

### Task 2: Protect the selected candidate draft

**Files:**
- Modify: `frontend/src/pages/PairSettingsPage.tsx:20-86`
- Test: `frontend/src/App.test.tsx`

- [ ] **Step 1: Track draft ownership and dirty state**

Add these state values after `notes`:

```tsx
const [draftPairId, setDraftPairId] = useState<string | null>(null)
const [draftDirty, setDraftDirty] = useState(false)
```

- [ ] **Step 2: Guard server-to-form synchronization**

Replace the existing selected-pair synchronization effect with:

```tsx
useEffect(() => {
  if (!selected) return
  if (selected.id === draftPairId && draftDirty) return
  setChecklist(selected.checklist ?? {})
  setNotes(selected.notes)
  setDraftPairId(selected.id)
  setDraftDirty(false)
}, [draftDirty, draftPairId, selected])
```

This preserves a dirty draft when refresh creates a new object for the same candidate, while a different candidate ID always initializes a fresh form.

- [ ] **Step 3: Mark user edits as dirty**

Update checkbox handling to:

```tsx
onChange={(event) => {
  setChecklist((current) => ({
    ...current,
    [key]: event.target.checked,
  }))
  setDraftDirty(true)
}}
```

Update the notes input to:

```tsx
<textarea
  value={notes}
  onChange={(event) => {
    setNotes(event.target.value)
    setDraftDirty(true)
  }}
/>
```

- [ ] **Step 4: Reconcile the draft after a successful save**

Immediately after updating `pairs` in `submitReview`, add:

```tsx
setChecklist(saved.checklist ?? {})
setNotes(saved.notes)
setDraftPairId(saved.id)
setDraftDirty(false)
```

Do not clear the draft in the error path, so failed submissions retain user input.

- [ ] **Step 5: Run the focused test and verify it passes**

Run:

```powershell
npm test -- --run --testNamePattern="preserves an unsaved review draft"
```

Expected: PASS.

- [ ] **Step 6: Run the existing review submission test**

Run:

```powershell
npm test -- --run --testNamePattern="lets a human review market equivalence"
```

Expected: PASS and the submitted review body still contains `status: exact` and the complement truth table.

- [ ] **Step 7: Commit the focused fix**

```powershell
git add -- frontend/src/App.test.tsx frontend/src/pages/PairSettingsPage.tsx
git commit -m "fix: preserve unsaved pair review drafts"
```

### Task 3: Verify the frontend

**Files:**
- Verify: `frontend/src/App.test.tsx`
- Verify: `frontend/src/pages/PairSettingsPage.tsx`

- [ ] **Step 1: Run the full frontend test suite**

Run:

```powershell
npm test -- --run
```

Expected: all frontend unit tests pass with no unhandled errors.

- [ ] **Step 2: Run lint**

Run:

```powershell
npm run lint
```

Expected: exit code 0 with no lint errors.

- [ ] **Step 3: Run the production build**

Run:

```powershell
npm run build
```

Expected: TypeScript and Vite complete successfully and generate `frontend/dist`.
