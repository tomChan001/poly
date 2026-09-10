# Private PostgreSQL SSL Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bundled desktop database ignore ambient PostgreSQL SSL requirements and deliver a validated Apple Silicon DMG.

**Architecture:** Keep `PostgresPaths.database_url` as the single connection source for Alembic and the application database layer, but make its local-only transport contract explicit with `ssl=disable`. Harden the installed-DMG smoke test by setting `PGSSLMODE=require`, proving that the packaged application overrides hostile ambient configuration.

**Tech Stack:** Python 3.12, asyncpg, SQLAlchemy, Alembic, pytest, GitHub Actions, Tauri macOS packaging

---

### Task 1: Isolate the private database URL from ambient SSL settings

**Files:**
- Modify: `backend/tests/unit/desktop/test_postgres.py:156`
- Modify: `backend/app/desktop/postgres.py:41`

- [ ] **Step 1: Update the URL contract test so it requires SSL to be disabled**

```python
def test_database_url_uses_encoded_unix_socket_and_poly_database(
    tmp_path: Path,
) -> None:
    paths = PostgresPaths.for_test(tmp_path / "path with spaces")
    encoded_socket = quote(str(paths.socket_dir), safe="")

    assert paths.database_url == (
        "postgresql+asyncpg://poly@/poly?"
        f"host={encoded_socket}&port=5432&ssl=disable"
    )
```

- [ ] **Step 2: Run the focused test and verify the RED state**

```powershell
uv run --frozen pytest backend/tests/unit/desktop/test_postgres.py::test_database_url_uses_encoded_unix_socket_and_poly_database -q
```

Expected: FAIL because the actual URL ends at `port=5432`.

- [ ] **Step 3: Add the local-only SSL setting to the URL source**

```python
@property
def database_url(self) -> str:
    socket = quote(str(self.socket_dir), safe="")
    return (
        "postgresql+asyncpg://poly@/poly?"
        f"host={socket}&port=5432&ssl=disable"
    )
```

- [ ] **Step 4: Run the desktop PostgreSQL unit tests**

```powershell
uv run --frozen pytest backend/tests/unit/desktop/test_postgres.py -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add -- backend/app/desktop/postgres.py backend/tests/unit/desktop/test_postgres.py
git commit -m "Isolate desktop PostgreSQL from ambient SSL settings"
```

### Task 2: Reproduce the hostile environment in the installed-DMG smoke

**Files:**
- Modify: `packaging/macos/tests/test_ci_documentation_contract.py:198`
- Modify: `.github/workflows/macos-desktop.yml:354`

- [ ] **Step 1: Require the smoke stage to force ambient SSL**

Add this assertion inside `test_smoke_uses_an_isolated_home_installed_dmg_and_command_guards`:

```python
assert "export PGSSLMODE=require" in smoke
```

- [ ] **Step 2: Run the focused test and verify the RED state**

```powershell
uv run --frozen pytest packaging/macos/tests/test_ci_documentation_contract.py::test_smoke_uses_an_isolated_home_installed_dmg_and_command_guards -q
```

Expected: FAIL because the smoke stage does not set `PGSSLMODE=require`.

- [ ] **Step 3: Harden the installed-DMG smoke environment**

Add the final export beside the existing isolated runtime environment:

```bash
export HOME="$SMOKE_HOME"
export PATH="$GUARD_DIR:/usr/bin:/bin:/usr/sbin:/sbin"
export POLY_GUARD_LOG="$GUARD_LOG"
export PGSSLMODE=require
```

- [ ] **Step 4: Run the complete CI contract suite**

```powershell
uv run --frozen pytest packaging/macos/tests/test_ci_documentation_contract.py -q
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add -- .github/workflows/macos-desktop.yml packaging/macos/tests/test_ci_documentation_contract.py
git commit -m "Test macOS desktop startup under required SSL"
```

### Task 3: Verify the local change set

**Files:**
- Verify: `backend/app/desktop/postgres.py`
- Verify: `backend/tests/unit/desktop/test_postgres.py`
- Verify: `.github/workflows/macos-desktop.yml`
- Verify: `packaging/macos/tests/test_ci_documentation_contract.py`

- [ ] **Step 1: Run all desktop backend tests**

```powershell
uv run --frozen pytest backend/tests/unit/desktop -q
```

Expected: all platform-applicable tests PASS; macOS-only tests may be skipped.

- [ ] **Step 2: Run macOS distribution and workflow contract tests**

```powershell
uv run --frozen pytest packaging/macos/tests/test_distribution_contract.py packaging/macos/tests/test_ci_documentation_contract.py -q
```

Expected: all platform-applicable tests PASS.

- [ ] **Step 3: Run lint and whitespace validation**

```powershell
uv run --frozen ruff check backend/app/desktop/postgres.py backend/tests/unit/desktop/test_postgres.py packaging/macos/tests/test_ci_documentation_contract.py
git diff --check HEAD~2
```

Expected: both commands exit successfully without diagnostics.

### Task 4: Build and deliver the Apple Silicon DMG

**Files:**
- Output: `C:/Users/ai4c_/Desktop/Poly-Apple-Silicon-$runId/Poly_0.1.0_aarch64.dmg`

- [ ] **Step 1: Push and dispatch the macOS workflow**

```powershell
git push origin HEAD:codex/macos-smoke-bounded
& 'C:\Program Files\GitHub CLI\gh.exe' workflow run macos-desktop.yml --ref codex/macos-smoke-bounded
```

Expected: both operations succeed.

- [ ] **Step 2: Resolve and monitor the dispatched run**

```powershell
$run = & 'C:\Program Files\GitHub CLI\gh.exe' run list --workflow macos-desktop.yml --branch codex/macos-smoke-bounded --event workflow_dispatch --limit 1 --json databaseId,url,status,headSha | ConvertFrom-Json
$runId = $run[0].databaseId
$run[0]
```

Expected: the newest run uses current HEAD and the `Poly-macos-arm64` job passes, including the installed-DMG smoke under `PGSSLMODE=require`.

- [ ] **Step 3: Download the Apple Silicon artifact**

Use the `$runId` resolved in Step 2:

```powershell
$destination = "C:\Users\ai4c_\Desktop\Poly-Apple-Silicon-$runId"
New-Item -ItemType Directory -Path $destination | Out-Null
& 'C:\Program Files\GitHub CLI\gh.exe' run download $runId --name Poly-macos-arm64 --dir $destination
```

Expected: the destination contains exactly one Apple Silicon DMG.

- [ ] **Step 4: Verify and deliver the artifact**

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath "$destination\Poly_0.1.0_aarch64.dmg"
& 'C:\Program Files\7-Zip\7z.exe' t "$destination\Poly_0.1.0_aarch64.dmg"
```

Expected: a SHA-256 is reported and 7-Zip ends with `Everything is Ok`. The delivery message must identify the successful ARM job and state that the artifact is ad-hoc signed and not notarized.
