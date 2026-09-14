# macOS No-Password Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate Poly's system password dialogs while preserving native secret protection and accepting platform-credential re-entry after updates.

**Architecture:** An explicitly selected macOS desktop secret store uses a build-specific namespace and disables native keychain UI before every operation. Existing server storage and execution safety gates remain intact. Frontend guidance describes re-entry and storage errors honestly.

**Tech Stack:** Python 3.12, keyring native macOS backend, ctypes Security framework, FastAPI, React/Vitest, PyInstaller, GitHub macOS CI.

---

### Task 1: Native storage policy and desktop wiring

Files: create `backend/app/core/macos_secrets.py` and `backend/tests/unit/core/test_macos_secrets.py`; modify `backend/app/core/secrets.py` only as needed for an explicit backend dependency; modify `backend/app/core/config.py`, `backend/app/container.py`, `backend/app/desktop/runtime.py` for desktop-only selection; extend desktop/container tests.

- [ ] Write failing tests: `assert service_for_executable(binary) == service_for_executable(binary)` across process restarts; distinct bytes must yield distinct service strings. Verify service starts with `com.poly.desktop.integrations.no-ui.v1.` and never equals the legacy service. Inject a fake native backend and UI-disabler; record `['disable', 'get']`, `['disable', 'set']`, `['disable', 'delete']`. If disabling fails, assert no backend operation runs. Synthetic secret and backend exception text must not occur in `str(SecretStorageError)`.
- [ ] Run `python -m pytest backend/tests/unit/core/test_macos_secrets.py -q --basetemp=.tmp/mac-red`; verify failures describe absent policy.
- [ ] Implement the bounded policy: `hashlib.file_digest(binary.open('rb'), 'sha256').hexdigest()` for namespace; native `Keyring` backend only; `ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security').SecKeychainSetUserInteractionAllowed` uses `c_bool` argument, `c_int32` result and must return zero before backend calls. Every operation disables UI and never restores it. Errors are sanitized. Desktop opts in explicitly; server/non-macOS keeps current store.
- [ ] Run the new tests plus `backend/tests/unit/core/test_secrets.py`, `backend/tests/unit/desktop/test_runtime.py`, and affected API tests with a fresh worktree-local basetemp; review diff and commit only task files.

### Task 2: User-facing reconfiguration and errors

Files: `frontend/src/pages/IntegrationSettingsPage.tsx`, a focused adjacent test, and an error-message helper if needed.

- [ ] Write failing UI tests for Mac guidance and `credential storage unavailable`: saving must not show success and must render Chinese guidance, with no raw error values.
- [ ] Implement guidance: `Mac 桌面版不会请求系统密码。安装新版后，请重新填写平台 API 密钥；旧钥匙串记录会保留。` Scope the text to desktop/Mac if the current frontend has a trustworthy platform flag, otherwise label it clearly as Mac desktop behavior.
- [ ] Map the known sanitized storage error to `无法安全访问密钥存储，本次操作未完成。请稍后重试；不会弹出系统密码框。` Leave other existing error handling intact.
- [ ] Run `npm test` and `npm run lint` in frontend; commit only task files after review.

### Task 3: Disposable native acceptance and release

Files: new `packaging/macos/test-no-ui-keychain.py`, `.github/workflows/macos-desktop.yml`, packaging contract tests, runbook.

- [ ] Add a static test requiring CI to run the no-UI acceptance against a disposable test keychain before artifact delivery. Run it red.
- [ ] Implement a native test harness using a newly created keychain under RUNNER_TEMP with synthetic credentials. Bind the test-only backend to that explicit keychain path; never change default/search-list/login keychains. Confirm legacy sentinel unchanged, same binary reopens values, changed binary namespace is empty, locked keychain fails promptly without UI, then unlock/delete only the exact disposable target. Record statuses only, no values.
- [ ] Run applicable static and native tests, then broad regression tests. Perform independent spec review followed by code-quality/security review and resolve findings.
- [ ] Commit and dispatch the existing macOS workflow on the new branch. Follow ARM64 acceptance, verify source/artifact identity, copy the passing DMG into a new delivery folder, calculate SHA-256, and provide concise reconfiguration instructions. Do not overwrite previous delivery or touch the dirty primary worktree.

## Progress

- Requirements approved; worktree created at `.worktrees/macos-no-password-20260914` based on 3596735.
- Initial baseline used an inaccessible shared Windows pytest temp directory. Retry uses a dedicated worktree-local basetemp; no product change is needed for that environment issue.
