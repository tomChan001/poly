# Bounded macOS Smoke Process Enumeration

## Goal

Keep the installed-DMG smoke test's exact, bundle-owned runtime evidence while ensuring that process discovery always completes quickly on GitHub's macOS runners.

## Design

Replace the recursive `lsof +D` walk of the packaged Python and PostgreSQL tree with bounded process discovery:

1. Read the process table once, retaining PID, parent PID, and arguments.
2. Seed candidates whose arguments contain the exact installed runtime prefix.
3. Add descendants of those candidates so PostgreSQL workers remain attributable to the packaged runtime even when they rewrite their argument text.
4. Resolve each candidate's executable with the existing narrowly scoped `lsof -p` check and emit only executables under the exact runtime root.

The workflow will also place a short timeout around the whole installed-DMG smoke step. A timeout must still trigger cleanup, diagnostic collection, and a failed job rather than occupying the runner until the job-level timeout.

## Error Handling

Process-table or exact-executable lookup failures for a live candidate remain hard failures. Processes that exit between discovery and verification are treated as an expected race. Cleanup continues to restore and remove the temporary Keychain and detach the DMG.

## Testing

Contract tests will reject recursive `lsof +D`, verify descendant discovery with rewritten child arguments, and require the workflow smoke timeout. The full packaging test suite and lint checks must pass before pushing. The resulting arm64 workflow must pass through installed-DMG smoke testing and upload the architecture-specific DMG.
