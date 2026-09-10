# Safe Migration Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a privacy-safe Apple Silicon diagnostic build that records a bounded migration failure fingerprint before the existing sanitized failure event.

**Architecture:** Construct allowlisted Python failure metadata in a focused diagnostics module, then inject that sink into `DesktopRuntime` so ordering and failure isolation are directly testable. Keep the Rust file-security boundary intact, add a fixed shell-startup marker, and make the installed-DMG smoke prove that the marker reaches the private log.

**Tech Stack:** Python 3.12, pytest, JSON, Tauri/Rust, Cargo, GitHub Actions, PyInstaller, PostgreSQL 16

---

### Task 1: Build the allowlisted Python diagnostic encoder

**Files:**
- Create: `backend/app/desktop/diagnostics.py`
- Create: `backend/tests/unit/desktop/test_diagnostics.py`

- [ ] **Step 1: Write failing privacy and bounds tests**

Create `backend/tests/unit/desktop/test_diagnostics.py`. Cover an exception whose
message contains a database URL, token, SQL, parameter, and user path; a wrapped
driver exception with `sqlstate="08006"` and `errno=13`; an eight-node cyclic
cause graph; invalid SQLSTATE/errno values; one-line flushing; and a stream whose
`write` raises. Assert the hostile message fragments never occur in encoded or
written output and the exception chain contains no more than four entries.

Use this representative assertion for the primary case:

```python
encoded = encode_runtime_failure_diagnostic(error, RuntimeState.MIGRATING)
assert json.loads(encoded) == {
    "diagnostic_schema": 1,
    "event": "runtime_failure",
    "phase": "migrating",
    "exception_chain": [
        f"{WrapperFailure.__module__}.WrapperFailure",
        f"{DriverFailure.__module__}.DriverFailure",
    ],
    "sqlstate": "08006",
    "errno": 13,
}
assert "postgresql://" not in encoded
assert "SELECT secret" not in encoded
```

- [ ] **Step 2: Run the new tests and verify the module is missing**

Run:

```powershell
uv run --frozen pytest --basetemp .tmp/pytest-safe-diag-red backend/tests/unit/desktop/test_diagnostics.py -q
```

Expected: collection fails with `ModuleNotFoundError` for
`backend.app.desktop.diagnostics`.

- [ ] **Step 3: Implement the constructed encoder and best-effort writer**

Create `backend/app/desktop/diagnostics.py` with this complete implementation:

```python
from __future__ import annotations

import json
import re
import sys
from collections import deque
from typing import TextIO

from backend.app.desktop.protocol import RuntimeState

DIAGNOSTIC_SCHEMA = 1
MAX_EXCEPTION_CHAIN = 4
_TYPE_NAME = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,15}\Z"
)
_SQLSTATE = re.compile(r"[0-9A-Z]{5}\Z")
_MIN_ERRNO = -(2**31)
_MAX_ERRNO = 2**31 - 1


def _attribute(error: BaseException, name: str) -> object | None:
    try:
        return getattr(error, name, None)
    except Exception:  # noqa: BLE001 - diagnostics cannot alter failure handling
        return None


def _exception_graph(error: BaseException) -> list[BaseException]:
    pending: deque[BaseException] = deque([error])
    found: list[BaseException] = []
    seen: set[int] = set()
    while pending and len(found) < MAX_EXCEPTION_CHAIN:
        current = pending.popleft()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        for name in ("orig", "__cause__", "__context__"):
            related = _attribute(current, name)
            if isinstance(related, BaseException) and id(related) not in seen:
                pending.append(related)
    return found


def _type_name(error: BaseException) -> str:
    error_type = type(error)
    module = getattr(error_type, "__module__", None)
    qualified = getattr(error_type, "__qualname__", None)
    if not isinstance(module, str) or not isinstance(qualified, str):
        return "unknown"
    candidate = f"{module}.{qualified}"
    if len(candidate) > 192 or _TYPE_NAME.fullmatch(candidate) is None:
        return "unknown"
    return candidate


def encode_runtime_failure_diagnostic(
    error: BaseException, phase: RuntimeState
) -> str:
    chain = _exception_graph(error)
    payload: dict[str, object] = {
        "diagnostic_schema": DIAGNOSTIC_SCHEMA,
        "event": "runtime_failure",
        "phase": phase.value,
        "exception_chain": [_type_name(item) for item in chain],
    }
    for item in chain:
        sqlstate = _attribute(item, "sqlstate")
        if isinstance(sqlstate, str) and _SQLSTATE.fullmatch(sqlstate):
            payload["sqlstate"] = sqlstate
            break
    for item in chain:
        errno = _attribute(item, "errno")
        if type(errno) is int and _MIN_ERRNO <= errno <= _MAX_ERRNO:
            payload["errno"] = errno
            break
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def write_runtime_failure_diagnostic(
    error: BaseException,
    phase: RuntimeState,
    *,
    stream: TextIO | None = None,
) -> None:
    try:
        target = stream if stream is not None else sys.stderr
        target.write(encode_runtime_failure_diagnostic(error, phase) + "\n")
        target.flush()
    except Exception:  # noqa: BLE001 - diagnostics cannot replace the real failure
        return
```

- [ ] **Step 4: Run the focused tests**

```powershell
uv run --frozen pytest --basetemp .tmp/pytest-safe-diag-green backend/tests/unit/desktop/test_diagnostics.py -q
```

Expected: every diagnostics test passes.

- [ ] **Step 5: Commit the encoder and tests**

```powershell
git add backend/app/desktop/diagnostics.py backend/tests/unit/desktop/test_diagnostics.py
git commit -m "Add privacy-safe runtime failure diagnostics"
```

### Task 2: Emit diagnostics before cleanup and the public failure

**Files:**
- Modify: `backend/app/desktop/runtime.py:65-145,214-226`
- Modify: `backend/tests/unit/desktop/test_runtime.py:15-30,235-280,452-475`

- [ ] **Step 1: Write failing runtime ordering and sink-isolation tests**

Add a `diagnose` argument to the existing `make_runtime` helper. In
`test_migration_failure_stops_database_and_leaves_marker`, use a sink that appends
`diagnostic:migrating` to `trace`, then require this order:

```python
assert trace == [
    "marker.create",
    "postgres.start",
    "migrate",
    "diagnostic:migrating",
    "postgres.stop",
]
```

Add `test_diagnostic_sink_failure_preserves_migration_failure`: its migration
raises, its sink raises `OSError`, and it asserts the exact public
`migration_failed` fields, `postgres.stop`, and the retained unclean marker.

- [ ] **Step 2: Run the migration failure selection and verify failure**

```powershell
uv run --frozen pytest --basetemp .tmp/pytest-runtime-diag-red backend/tests/unit/desktop/test_runtime.py -q -k "migration_failure"
```

Expected: failure because `DesktopRuntime` has no injectable diagnostic sink and
the diagnostic step is absent.

- [ ] **Step 3: Inject and invoke the diagnostic sink**

In `runtime.py`, import `write_runtime_failure_diagnostic`, define
`FailureDiagnosticSink = Callable[[BaseException, RuntimeState], None]`, and add
this constructor field:

```python
failure_diagnostic_sink: FailureDiagnosticSink | None = None,

self.failure_diagnostic_sink = (
    failure_diagnostic_sink or write_runtime_failure_diagnostic
)
```

Replace the broad startup handler with:

```python
except Exception as error:  # noqa: BLE001 - sanitize every orchestration boundary
    try:
        self.failure_diagnostic_sink(error, phase)
    except Exception as diagnostic_error:  # noqa: BLE001
        del diagnostic_error
    await self._finish_cleanup(clean=False, disable_reason=None)
    if phase is RuntimeState.PREPARING_DATABASE:
        return await self._failure(
            "database_unavailable", "private database is unavailable"
        )
    if phase is RuntimeState.MIGRATING:
        return await self._failure("migration_failed", "database migration failed")
    return await self._failure(
        "runtime_unavailable", "desktop services could not start"
    )
```

- [ ] **Step 4: Run focused and complete desktop tests**

```powershell
uv run --frozen pytest --basetemp .tmp/pytest-runtime-diag-green backend/tests/unit/desktop/test_runtime.py -q -k "migration_failure"
uv run --frozen pytest --basetemp .tmp/pytest-desktop-diag-full backend/tests/unit/desktop -q
```

Expected: both commands complete with no failures.

- [ ] **Step 5: Commit runtime integration**

```powershell
git add backend/app/desktop/runtime.py backend/tests/unit/desktop/test_runtime.py
git commit -m "Record desktop startup failure fingerprints"
```

### Task 3: Mark successful Rust diagnostic initialization

**Files:**
- Modify: `src-tauri/src/lib.rs:72-120,359-700`

- [ ] **Step 1: Write a failing reporter wiring test**

Add `tests::bounded_desktop_reporter_writes_fixed_startup_marker` in
`src-tauri/src/lib.rs`. Build a unique application-support root below
`std::env::temp_dir()` from the PID and current UNIX-epoch nanoseconds, configure
`BoundedDesktopReporter`, drop it, and require exact content at
`Poly/logs/runtime.stderr.log`:

```rust
#[test]
fn bounded_desktop_reporter_writes_fixed_startup_marker() {
    let nonce = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!(
        "poly-desktop-diagnostic-marker-{}-{nonce}",
        std::process::id()
    ));
    fs::create_dir_all(&root).unwrap();
    let reporter = BoundedDesktopReporter::new();

    reporter.configure(&root);
    drop(reporter);

    let path = root.join("Poly/logs/runtime.stderr.log");
    assert_eq!(fs::read_to_string(&path).unwrap(), "desktop_diagnostics_ready\n");
    fs::remove_dir_all(root).unwrap();
}
```

On Unix, extend this test with `PermissionsExt` and assert mode `0o600`.

- [ ] **Step 2: Run the exact marker test and verify it fails**

```powershell
cargo test --locked --manifest-path src-tauri/Cargo.toml --lib tests::bounded_desktop_reporter_writes_fixed_startup_marker -- --exact
```

Expected: the assertion fails because the current log is empty.

- [ ] **Step 3: Write the fixed marker at the reporter boundary**

Add near `BoundedDesktopReporter`:

```rust
const DESKTOP_DIAGNOSTICS_READY_MARKER: &str = "desktop_diagnostics_ready\n";
```

Change `configure` so it retains the log only after the marker succeeds:

```rust
fn configure(&self, application_support: &std::path::Path) {
    let configured = runtime::process::DiagnosticLog::under_application_support(
        application_support,
    )
    .and_then(|mut log| {
        log.write_redacted(DESKTOP_DIAGNOSTICS_READY_MARKER)?;
        Ok(log)
    });
    match configured {
        Ok(log) => *self.log.lock().expect("diagnostic log lock poisoned") = Some(log),
        Err(_) => {
            self.dropped
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        }
    }
}
```

Do not place the marker in `DiagnosticLog::under_application_support`; the
supervisor also opens that type and would create misleading duplicate markers.

- [ ] **Step 4: Format and run marker plus diagnostic security tests**

```powershell
cargo fmt --manifest-path src-tauri/Cargo.toml -- --check
cargo test --locked --manifest-path src-tauri/Cargo.toml --lib tests::bounded_desktop_reporter_writes_fixed_startup_marker -- --exact
cargo test --locked --manifest-path src-tauri/Cargo.toml --lib diagnostic
```

Expected: formatting completes and both test selections pass.

- [ ] **Step 5: Commit the startup marker**

```powershell
git add src-tauri/src/lib.rs
git commit -m "Mark desktop diagnostic log initialization"
```

### Task 4: Make installed-DMG smoke validate real log evidence

**Files:**
- Modify: `.github/workflows/macos-desktop.yml:230-355`
- Modify: `packaging/macos/tests/test_ci_documentation_contract.py:200-310`

- [ ] **Step 1: Write a failing workflow contract test**

Add `test_installed_dmg_smoke_asserts_desktop_diagnostics_ready_marker`. Extract
the installed-DMG smoke block and assert:

```python
log_assignment = (
    'RUNTIME_STDERR_LOG="${SMOKE_HOME}/Library/Application '
    'Support/Poly/logs/runtime.stderr.log"'
)
marker_assertion = (
    "/usr/bin/grep -Fxq 'desktop_diagnostics_ready' "
    '"${RUNTIME_STDERR_LOG}"'
)
launch = '/usr/bin/open -n "${INSTALLED_APP}"'

assert log_assignment in smoke
assert marker_assertion in smoke
launch_position = smoke.index(launch)
marker_position = smoke.index(marker_assertion)
quit_position = smoke.index("tell application id", launch_position)
assert launch_position < marker_position < quit_position
assert "PGSSLMODE" not in smoke
assert '/bin/cp "${RUNTIME_STDERR_LOG}"' in smoke
```

Remove the old positive assertion for `export PGSSLMODE=require`; Rust clears
that variable before launching Python, so it did not test the packaged behavior.

- [ ] **Step 2: Run the exact contract and verify it fails**

```powershell
uv run --frozen pytest --basetemp .tmp/pytest-ci-diag-red packaging/macos/tests/test_ci_documentation_contract.py::test_installed_dmg_smoke_asserts_desktop_diagnostics_ready_marker -q
```

Expected: failure because the workflow does not yet define or assert the marker.

- [ ] **Step 3: Update the installed-DMG smoke**

Define the path beside `RUNTIME_EXECUTABLE`:

```bash
RUNTIME_STDERR_LOG="${SMOKE_HOME}/Library/Application Support/Poly/logs/runtime.stderr.log"
```

Have `copy_smoke_diagnostics` copy the current and rotated safe logs when present:

```bash
/bin/cp "${RUNTIME_STDERR_LOG}" "${DIAGNOSTICS_DIR}/runtime.stderr.log" 2>/dev/null || true
/bin/cp "${RUNTIME_STDERR_LOG}.1" "${DIAGNOSTICS_DIR}/runtime.stderr.log.1" 2>/dev/null || true
```

Remove `export PGSSLMODE=require`. Immediately after the first listener assertion
and before quitting the app, add:

```bash
test -f "${RUNTIME_STDERR_LOG}"
/usr/bin/grep -Fxq 'desktop_diagnostics_ready' "${RUNTIME_STDERR_LOG}"
```

- [ ] **Step 4: Run workflow and portable macOS contract tests**

```powershell
uv run --frozen pytest --basetemp .tmp/pytest-ci-diag-green packaging/macos/tests/test_ci_documentation_contract.py -q
uv run --frozen pytest --basetemp .tmp/pytest-macos-diag-full packaging/macos/tests/test_distribution_contract.py packaging/macos/tests/test_ci_documentation_contract.py -q --deselect packaging/macos/tests/test_distribution_contract.py::DistributionContractTests::test_committed_notices_are_generated_and_checkable_without_bundle --deselect packaging/macos/tests/test_distribution_contract.py::DistributionContractTests::test_notice_generator_uses_pinned_macos_cpython_license
```

Expected: both commands complete with no failures. The two notice tests are
deselected locally only because Windows line endings differ; native CI runs them.

- [ ] **Step 5: Commit the corrected smoke evidence**

```powershell
git add .github/workflows/macos-desktop.yml packaging/macos/tests/test_ci_documentation_contract.py
git commit -m "Validate packaged desktop diagnostics"
```

### Task 5: Verify and deliver the native Apple Silicon diagnostic DMG

**Files:**
- Verify only; no source changes expected

- [ ] **Step 1: Run all local completion gates**

```powershell
uv run --frozen pytest --basetemp .tmp/pytest-safe-diag-final backend/tests/unit/desktop -q
uv run --frozen pytest --basetemp .tmp/pytest-safe-diag-macos-final packaging/macos/tests/test_distribution_contract.py packaging/macos/tests/test_ci_documentation_contract.py -q --deselect packaging/macos/tests/test_distribution_contract.py::DistributionContractTests::test_committed_notices_are_generated_and_checkable_without_bundle --deselect packaging/macos/tests/test_distribution_contract.py::DistributionContractTests::test_notice_generator_uses_pinned_macos_cpython_license
uv run --frozen ruff check backend/app/desktop backend/tests/unit/desktop
cargo fmt --manifest-path src-tauri/Cargo.toml -- --check
cargo clippy --locked --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --locked --manifest-path src-tauri/Cargo.toml
git diff --check main...HEAD
```

Expected: every command exits zero, with no test failures, lint errors, format
changes, Clippy warnings, or whitespace errors.

- [ ] **Step 2: Push the branch and start native CI**

```powershell
git status --short
git push -u origin codex/macos-smoke-bounded
gh workflow run macos-desktop.yml --ref codex/macos-smoke-bounded
```

Expected: only local `.tmp/` remains untracked, the push succeeds, and GitHub
accepts a new workflow run.

- [ ] **Step 3: Wait for and inspect the Apple Silicon job**

Resolve the new ID with `gh run list --workflow macos-desktop.yml --branch
codex/macos-smoke-bounded`, then run:

```powershell
gh run watch RUN_ID --exit-status
gh run view RUN_ID --json status,conclusion,url,jobs
```

Expected: `Poly-macos-arm64` succeeds through packaged runtime verification,
bundle audit, installed-DMG startup, diagnostic marker assertion, and artifact
upload. This build gathers evidence; it does not yet prove the user's migration
fault is fixed.

- [ ] **Step 4: Download and integrity-check the Apple Silicon DMG**

```powershell
gh run download RUN_ID -n Poly-macos-arm64 -D .tmp/Poly-Apple-Silicon-RUN_ID
Get-FileHash -Algorithm SHA256 .tmp/Poly-Apple-Silicon-RUN_ID/*.dmg
& 'C:\Program Files\7-Zip\7z.exe' t .tmp/Poly-Apple-Silicon-RUN_ID/*.dmg
```

Expected: one aarch64 DMG is downloaded, SHA-256 is printed, and 7-Zip reports
`Everything is Ok`.

- [ ] **Step 5: Copy the validated DMG to a user-facing folder**

Create a new run-ID-specific Desktop folder and copy the validated DMG there.
Report its absolute path, SHA-256, and CI links. Tell the user to fully quit Poly,
replace `/Applications/Poly.app`, launch once, reveal diagnostics, and return
`runtime.stderr.log`. State clearly that the build is diagnostic and that the
root-cause data fix follows the captured fingerprint. Do not modify or delete the
user's PostgreSQL directory.
