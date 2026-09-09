# macOS Runtime Initializing Protocol Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore Mac desktop startup by making the packaged Python runtime emit the required first `initializing` protocol event.

**Architecture:** Keep the Python child process authoritative for lifecycle events and keep the Rust supervisor strict. Emit one `initializing` event after the first start command is validated and before `DesktopRuntime.start()` can emit `preparing_database`.

**Tech Stack:** Python 3.12, asyncio, pytest, Rust/Tauri, GitHub Actions macOS arm64

---

### Task 1: Lock the startup event ordering with a regression test

**Files:**
- Modify: `backend/tests/unit/desktop/test_runtime.py:880`
- Test: `backend/tests/unit/desktop/test_runtime.py`

- [ ] **Step 1: Write the failing test**

Add an event capture to the valid parent-EOF stdio test and make the fake runtime verify that `initializing` has already been written before its `start()` method runs:

```python
protocol_output: list[str] = []

class Runtime:
    async def start(self, command: StartCommand) -> RuntimeEvent:
        assert len(command.launch_token) == 43
        assert [json.loads(line)["state"] for line in protocol_output] == [
            "initializing"
        ]
        return RuntimeEvent(
            RuntimeState.READY,
            {"port": 49152, "bootstrap_path": "/desktop/bootstrap/safe"},
        )

result = await desktop_main.run_stdio(
    runtime=cast(DesktopRuntime, Runtime()),
    line_reader=lambda: next(lines),
    event_writer=protocol_output.append,
)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
uv run pytest backend/tests/unit/desktop/test_runtime.py::test_stdio_parent_eof_stops_runtime_without_leaking_token -q
```

Expected: FAIL because `protocol_output` is empty when `Runtime.start()` is entered.

### Task 2: Emit the required initializing event

**Files:**
- Modify: `backend/app/desktop/__main__.py:117-140`
- Test: `backend/tests/unit/desktop/test_runtime.py`

- [ ] **Step 1: Add the minimal protocol emission**

After the first command has been successfully parsed, emit the required lifecycle event before constructing or starting the active runtime:

```python
await emit(RuntimeEvent(RuntimeState.INITIALIZING, {}))
active_runtime = runtime or DesktopRuntime(event_sink=emit)
```

Do not emit `initializing` for invalid commands and do not loosen the Rust transition rules.

- [ ] **Step 2: Run the focused test and verify GREEN**

Run:

```powershell
uv run pytest backend/tests/unit/desktop/test_runtime.py::test_stdio_parent_eof_stops_runtime_without_leaking_token -q
```

Expected: `1 passed`.

- [ ] **Step 3: Run the desktop runtime regression suite**

Run:

```powershell
uv run pytest backend/tests/unit/desktop -q
uv run ruff check backend/app/desktop/__main__.py backend/tests/unit/desktop/test_runtime.py
```

Expected: all tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 4: Commit the protocol fix**

```powershell
git add -- backend/app/desktop/__main__.py backend/tests/unit/desktop/test_runtime.py
git commit -m "Fix macOS runtime startup protocol"
```

### Task 3: Build and validate the replacement Apple Silicon DMG

**Files:**
- Verify: `.github/workflows/macos-desktop.yml`
- Verify: `packaging/macos/verify-bundle.sh`

- [ ] **Step 1: Push the fix branch and start the macOS workflow**

```powershell
git push origin codex/macos-smoke-bounded
& "C:\Program Files\GitHub CLI\gh.exe" workflow run macos-desktop.yml --repo tomChan001/poly --ref codex/macos-smoke-bounded
```

Expected: GitHub creates a new workflow-dispatch run for the fix commit.

- [ ] **Step 2: Verify the Apple Silicon job**

Poll only `Poly-macos-arm64`. Confirm the packaged runtime build, bundle audit, installed-app smoke, and artifact upload steps all succeed. If a new failure appears, collect its diagnostics and return to a new RED/GREEN bugfix cycle before delivery.

- [ ] **Step 3: Download and verify the DMG**

Download `Poly-macos-arm64` into a new desktop directory, then run:

```powershell
$headSha = git rev-parse HEAD
$runId = (& "C:\Program Files\GitHub CLI\gh.exe" run list --repo tomChan001/poly --workflow macos-desktop.yml --event workflow_dispatch --json databaseId,headSha | ConvertFrom-Json | Where-Object headSha -eq $headSha | Select-Object -First 1).databaseId
$downloadDirectory = "C:\Users\ai4c_\Desktop\Poly-Apple-Silicon-$runId"
New-Item -ItemType Directory -Path $downloadDirectory
& "C:\Program Files\GitHub CLI\gh.exe" run download $runId --repo tomChan001/poly -n Poly-macos-arm64 -D $downloadDirectory
$dmg = Get-ChildItem -LiteralPath $downloadDirectory -Filter *.dmg -File
Get-FileHash -Algorithm SHA256 -LiteralPath $dmg.FullName
& "C:\Program Files\7-Zip\7z.exe" t $dmg.FullName
```

Expected: one `Poly_0.1.0_aarch64.dmg`, a recorded SHA-256 hash, and `Everything is Ok`.

- [ ] **Step 4: Deliver the replacement build**

Provide the clickable local DMG path, file size, SHA-256, and GitHub run URL. State that this validation build is ad-hoc signed and not Apple-notarized.
