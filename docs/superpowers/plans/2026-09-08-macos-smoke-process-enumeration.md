# Bounded macOS Smoke Process Enumeration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make installed-DMG runtime discovery exact and fast enough to complete reliably on GitHub macOS runners.

**Architecture:** Replace recursive filesystem-based `lsof +D` discovery with one process-table snapshot. Seed candidates from the exact runtime path in their arguments, expand through parent-child relationships, then retain the existing per-PID executable-path verification.

**Tech Stack:** Bash, Python `unittest`/pytest-compatible contract tests, GitHub Actions YAML.

---

### Task 1: Replace recursive runtime discovery

**Files:**
- Modify: `packaging/macos/tests/test_distribution_contract.py`
- Modify: `packaging/macos/verify-bundle.sh`

- [ ] **Step 1: Write the failing bounded-discovery test**

Add a test that supplies a process-table stub containing a runtime process, a child with rewritten PostgreSQL arguments, and an unrelated process. Supply an `lsof` stub that maps the two owned PIDs to real fixture executables. Assert both owned PIDs are emitted, the unrelated PID is absent, and `verify-bundle.sh` contains neither `+D` nor another recursive path scan.

```python
self.assertNotIn('+D "${runtime_directory}"', verifier)
self.assertIn('pid=,ppid=,args=', verifier)
self.assertEqual(result.stdout.splitlines(), ["4242", "4243"])
```

- [ ] **Step 2: Run the focused test and confirm it fails**

Run:

```text
uv run pytest packaging/macos/tests/test_distribution_contract.py -k bounded_process_tree -q
```

Expected: failure because the script still uses `lsof +D` and does not expand candidate descendants.

- [ ] **Step 3: Implement snapshot and descendant expansion**

Change `enumerate_process_paths` to capture `pid`, `ppid`, and `args`, seed exact-prefix argument matches, and repeatedly add children of already-selected PIDs. Keep `process_executable` as the final exact-path verifier.

```bash
"${PS_BIN}" -ww -axo pid=,ppid=,args= >"${VERIFY_TEMP}/ps-processes"
# Parse rows into a snapshot, seed prefix matches, then add descendants until stable.
# Verify only those candidates with process_executable before prefix filtering.
```

- [ ] **Step 4: Run focused and existing enumeration tests**

Run:

```text
uv run pytest packaging/macos/tests/test_distribution_contract.py -k "process_enumeration or bounded_process_tree" -q
```

Expected: all selected tests pass.

### Task 2: Bound the installed-DMG smoke step

**Files:**
- Modify: `packaging/macos/tests/test_ci_documentation_contract.py`
- Modify: `.github/workflows/macos-desktop.yml`

- [ ] **Step 1: Write the failing workflow contract test**

Require the installed-DMG smoke step to declare a ten-minute step timeout.

```python
workflow = read(WORKFLOW)
smoke_header = workflow.split(
    "- name: Install DMG into an isolated home and smoke test", 1
)[1].split("run: |", 1)[0]
assert "timeout-minutes: 10" in smoke_header
```

- [ ] **Step 2: Run the focused test and confirm it fails**

Run:

```text
uv run pytest packaging/macos/tests/test_ci_documentation_contract.py -k smoke_timeout -q
```

Expected: failure because the step has no timeout.

- [ ] **Step 3: Add the workflow timeout**

```yaml
- name: Install DMG into an isolated home and smoke test
  timeout-minutes: 10
```

- [ ] **Step 4: Run all macOS packaging contract tests and lint**

Run:

```text
uv run pytest packaging/macos/tests -q
uv run ruff check packaging/macos/tests
```

Expected: all tests and lint checks pass.

### Task 3: Ship and verify the arm64 artifact

**Files:**
- Commit only the two scripts, two tests, and this plan.

- [ ] **Step 1: Review the scoped diff and commit**

Stage only the owned files, confirm unrelated user changes remain unstaged, and commit the bounded enumeration fix.

- [ ] **Step 2: Push and dispatch the macOS workflow**

Push `main`, dispatch `.github/workflows/macos-desktop.yml`, and track only `Poly-macos-arm64`.

- [ ] **Step 3: Download and verify the DMG**

After the arm64 job succeeds, download `Poly-macos-arm64` to a fresh Desktop directory. Record the absolute path, byte size, and SHA-256 digest.
