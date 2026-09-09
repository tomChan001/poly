# macOS Runtime Initializing Protocol Fix

## Problem

The desktop runtime protocol requires the first child-process event to be
`initializing`. The packaged Python entry point currently validates the start
command and calls `DesktopRuntime.start()` without emitting that event.
`DesktopRuntime.start()` emits `preparing_database` first, so the Rust
supervisor correctly rejects the stream as an invalid state transition and the
UI displays `本地服务通信失败`.

## Design

The Python stdio entry point remains the authoritative producer of lifecycle
events. After it has successfully parsed and validated the first `start`
command, it will emit exactly one `initializing` event before invoking
`DesktopRuntime.start()`.

The Rust supervisor will remain strict. It will not synthesize a missing event
or accept `preparing_database` directly from the idle state. Invalid or
out-of-order child messages will continue to fail closed.

## Error Handling

Invalid first commands continue to produce only the existing sanitized
`invalid_start_command` failure. The new event is emitted only after a valid
start command, so malformed input cannot advance the lifecycle state.

## Verification

A Python regression test will exercise `run_stdio()` and assert that a valid
start produces `initializing` before the runtime's subsequent lifecycle
events. Existing Python protocol tests and Rust supervisor tests must remain
green. A new Apple Silicon GitHub build must pass the packaged-runtime, bundle,
and installed-app startup checks before its DMG is delivered.
