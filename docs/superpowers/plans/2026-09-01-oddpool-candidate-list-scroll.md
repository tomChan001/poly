# Oddpool Candidate List Scroll Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the Oddpool candidate header visible while candidate rows scroll inside a viewport-bounded list on desktop and mobile.

**Architecture:** Add one semantic scroll-region wrapper below the existing candidate header, then constrain only that wrapper with responsive CSS. Preserve the right review panel and all candidate loading, selection, refresh, and review behavior.

**Tech Stack:** React 19, TypeScript, CSS, Vitest, Testing Library, Vite, oxlint

---

### Task 1: Add the independently scrolling candidate region

**Files:**
- Modify: `frontend/src/pages/PairSettingsPage.tsx`
- Modify: `frontend/src/App.css`
- Test: `frontend/src/App.test.tsx`

- [ ] **Step 1: Write the failing structure test**

In the existing `shows real runtime state and lets a human review market equivalence` test, after
opening the audit view and waiting for the selected pair, add these assertions:

```tsx
const candidateScrollRegion = screen.getByRole('region', {
  name: 'Oddpool 候选内容',
})
const candidateButton = screen.getByRole('button', { name: /示例互补市场/ })
const candidateHeading = screen.getByRole('heading', { name: 'Oddpool 候选' })

expect(candidateScrollRegion).toHaveClass('pair-list-scroll')
expect(candidateScrollRegion).toContainElement(candidateButton)
expect(candidateScrollRegion).not.toContainElement(candidateHeading)
expect(candidateScrollRegion).toHaveAttribute('tabindex', '0')
```

This proves the fixed header is outside the candidate content region, the candidate rows are
inside it, and keyboard users can focus the scrolling region.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
npm test -- --testNamePattern "shows real runtime state and lets a human review market equivalence"
```

Expected: FAIL because no region named `Oddpool 候选内容` exists.

- [ ] **Step 3: Wrap candidate rows and the empty state**

In `frontend/src/pages/PairSettingsPage.tsx`, keep the existing header as a direct child of
`.pair-list`, then wrap both the empty state and mapped buttons:

```tsx
<section className="pair-list" aria-label="自动候选列表">
  <header><h3>Oddpool 候选</h3><span>{pairs.length}</span></header>
  <div
    className="pair-list-scroll"
    role="region"
    aria-label="Oddpool 候选内容"
    tabIndex={0}
  >
    {!loading && pairs.length === 0 && (
      <div className="pair-empty">暂无自动发现的待审核候选</div>
    )}
    {pairs.map((pair) => (
      <button
        key={pair.id}
        type="button"
        className={pair.id === selectedId ? 'pair-row selected' : 'pair-row'}
        onClick={() => { setSelectedId(pair.id); setMessage(null) }}
      >
        <strong>{pair.title}</strong>
        <span>{pair.kalshi_market_id} / {pair.polymarket_market_id}</span>
        <b className={`pair-status ${pair.status}`}>{pair.status.toUpperCase()}</b>
      </button>
    ))}
  </div>
</section>
```

Do not change keys, click handlers, loading conditions, selection classes, or candidate text.

- [ ] **Step 4: Add desktop and mobile scroll constraints**

In `frontend/src/App.css`, keep the left grid item stretched to the workspace height, make it a
vertical container, and add the internal scroll-region rules:

```css
.pair-list {
  min-width: 0;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--line);
}

.pair-list-scroll {
  min-height: 0;
  max-height: clamp(220px, calc(100dvh - 340px), 620px);
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}
```

Replace the existing one-line `.pair-list` rule rather than duplicating it. Inside the existing
`@media (max-width: 680px)` block, keep the current border override and add:

```css
.pair-list-scroll { max-height: min(42dvh, 360px); }
```

The left `.pair-list` stays stretched by the grid row, while the header remains outside the scroll
wrapper so it stays visible without sticky positioning. The internal `.pair-list-scroll` owns the
height cap and overflow behavior. The right `.review-panel` receives no height or overflow
changes.

- [ ] **Step 5: Run the focused test and verify GREEN**

Run:

```powershell
npm test -- --testNamePattern "shows real runtime state and lets a human review market equivalence"
```

Expected: PASS.

- [ ] **Step 6: Run the full frontend verification**

Run:

```powershell
npm test
npm run lint
npm run build
git diff --check
```

Expected: all tests pass, oxlint emits no errors, Vite production build succeeds, and
`git diff --check` emits no output.

- [ ] **Step 7: Commit the implementation**

```powershell
git add -- frontend/src/pages/PairSettingsPage.tsx frontend/src/App.css frontend/src/App.test.tsx
git commit -m "fix: constrain Oddpool candidate list height"
```

### Task 2: Verify viewport behavior visually

**Files:**
- Verify only; do not modify production files unless the visual checks expose a defect.

- [ ] **Step 1: Start the existing local frontend against the read-only backend**

Run the Vite server from `frontend/` without changing API or trading configuration:

```powershell
npm run dev -- --host 127.0.0.1
```

Use the URL printed by Vite. The backend must remain `read_only` with
`opening_enabled=false`.

- [ ] **Step 2: Verify desktop layout**

At a desktop viewport near `1440 × 900`, open the 审核 menu and confirm:

1. The `Oddpool 候选` header and count remain above the scroll region.
2. The scroll region has `scrollHeight > clientHeight` when many candidates exist.
3. The scroll region bottom is at or above the viewport bottom.
4. Scrolling the region changes its `scrollTop` without scrolling the page.
5. The right review panel has no new `overflow-y` or fixed height.

- [ ] **Step 3: Verify mobile layout**

At a mobile viewport near `390 × 844`, confirm:

1. The candidate list is above the review detail in the existing single-column layout.
2. The candidate content region is no taller than `42dvh` and no taller than `360px`.
3. Candidate rows scroll inside the region while the `Oddpool 候选` header remains visible.
4. The bottom navigation and review detail remain reachable.

- [ ] **Step 4: Re-run final checks after visual verification**

```powershell
npm test
npm run lint
npm run build
git status --short
```

Expected: tests, lint, and build pass; status shows no uncommitted implementation changes.
