# macOS credential access without password dialogs

## Approved requirement

The user approved no Mac/keychain password entry, including the first launch; re-entering platform API credentials is acceptable, including after future updates. Keep prior keychain entries untouched. Never fall back to plaintext or silently claim a failed save succeeded. The user explicitly approved implementation on 2026-09-14.

## Selected approach

Continue using the native macOS login keychain with all programmatic keychain interaction disabled for the desktop runtime process. Use a new service namespace, `com.poly.desktop.integrations.no-ui.v1.<executable-sha256>`, tied to the actual signed runtime executable bytes. The same installed binary reads its own saved entries after restart; a different binary begins with an empty namespace and requires platform credential re-entry. Never probe, migrate, or delete the old service. Normal server and non-macOS behavior remains unchanged.

Use Apple's Security framework `SecKeychainSetUserInteractionAllowed(false)` before every native keychain operation. Treat failure to install this protection as an error before touching keychain entries. Select a narrow native file-keychain backend directly, not environment-controlled keyring plugins. Do not re-enable interaction in this process. This is a legacy API, not an ACL bypass; a locked or denied keychain returns an error. Preserve the existing chunked-secret behavior and sanitized error boundary. Code inspection found the pinned keyring macOS adapter ignores explicit keychain paths and deletes before writes, so this policy uses native find/add/modify/delete functions with explicit test-keychain handles and non-destructive updates instead.

The alternative of keeping credentials only in memory was rejected because every restart would require re-entry. Plaintext persistence and broad ACL permissions are out of scope.

## Application behavior

- Integration settings explain in Chinese that the Mac desktop does not request a system password, that upgraded builds may need credentials re-entered, and that old entries are retained.
- Storage failure is rendered in Chinese, does not suggest entering a Mac password, and does not show success.
- Missing credentials leave existing readiness and real-ordering gates closed; public preview functionality is not used to bypass authenticated execution requirements.
- Never include values in logs, diagnostics, output artifacts, shell arguments, or reports. Tests use synthetic disposable values only.

## Validation and delivery

Use red/green unit tests for executable identity, restart stability, update isolation, native-backend selection, interaction-suppression ordering, suppressed-UI failure, error sanitization, and unchanged normal-server behavior. Add frontend tests for guidance and failed-save behavior. Run existing credential, API, desktop, security, packaging, and frontend tests.

Add an opt-in native macOS test using a disposable test keychain only; test missing/set/get/update/delete, persistence in a second process, locked-keychain fast failure, and sentinel preservation in the legacy service. Never target the developer or user's login keychain in tests. Because keychain creation may register a new keychain automatically, snapshot and restore the runner's original search list immediately after creation and in cleanup; never reassign its default keychain. Run the test on CI and preserve evidence. Build the ARM64 DMG from the reviewed commit, run the existing installed-artifact acceptance, and deliver only the verified ARM64 artifact. Retain the current ad-hoc/not-notarized distribution caveat. No Intel repair, live account calls, or real orders are authorized here.

## Limits

The installed frozen executable also performs its own CI synthetic set/restart-verify/delete round trip for both ad-hoc and signed builds using the existing isolated smoke keychain and guaranteed restoration. This is separate from the source-level fixture and avoids claiming that changing a synthetic identity file alone proves frozen executable behavior. Native locked-keychain and changed-namespace tests remain source-level; no two independently signed full Poly builds are compared automatically.

No code can read an item that the OS denies while also silently bypassing authorization. This design avoids requesting those old items; it does not grant new permissions to them. If the login keychain itself is unavailable, the app reports storage unavailable without asking for its password. Product source lives in an isolated worktree based on delivered commit 3596735.

Reference: https://developer.apple.com/documentation/security/keychains
