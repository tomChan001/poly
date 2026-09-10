# Safe Desktop Migration Diagnostics

## Problem

The installed Apple Silicon application reaches the database migration phase and
reports `migration_failed`, while `runtime.stderr.log` remains empty. The supplied
PostgreSQL data starts successfully, and a byte-for-byte copy migrates to the
current Alembic head outside the packaged application. The production runtime
currently catches every migration exception and replaces it with a fixed public
failure event without preserving any root-cause evidence.

The previous `PGSSLMODE=require` smoke assertion did not exercise the packaged
Python process because the Rust launcher clears the environment and does not
allow that variable through. SSL therefore remains only a disproven diagnostic
hypothesis, not an established production root cause.

## Goal

Produce one diagnostic Apple Silicon build that records enough non-sensitive
failure metadata to locate the real migration fault, without changing database
behavior or exposing user data, configuration, filesystem paths, SQL, or secrets.

## Design

### Diagnostic event

When `DesktopRuntime.start` catches an exception, it will synchronously write and
flush one compact JSON line to stderr before cleanup completes and before the
existing public `FAILED` event is emitted. The line uses a versioned schema and an
allowlist of fields:

```json
{"diagnostic_schema":1,"event":"runtime_failure","phase":"migrating","exception_chain":["sqlalchemy.exc.OperationalError","asyncpg.exceptions.ConnectionDoesNotExistError"],"sqlstate":"08006","errno":13}
```

Allowed values are constructed rather than redacted after the fact:

- `diagnostic_schema` is the integer constant `1`.
- `event` is the fixed string `runtime_failure`.
- `phase` is selected from the runtime's fixed state enum.
- `exception_chain` contains at most four fully qualified exception class names.
  Each name must match a bounded ASCII identifier grammar; invalid names become
  `unknown`.
- `sqlstate` is optional and accepted only when it is exactly five uppercase
  ASCII letters or digits.
- `errno` is optional and accepted only when it is a bounded integer and not a
  boolean.

The exception graph traversal is cycle-safe. It follows Python exception causes
and contexts plus a wrapped database driver's `orig` exception, without reading
messages or argument values.

### Privacy boundary

The diagnostic line must never contain:

- `str(exception)`, `repr(exception)`, exception arguments, or traceback text;
- SQL statements, bound parameters, database rows, or migration data;
- database URLs, usernames, home paths, filenames, or environment variables;
- authentication headers, cookies, tokens, passwords, API keys, or Keychain
  values.

Diagnostic write or flush failure is ignored and cannot replace the original
runtime failure, change cleanup order, or alter the user-facing error event.
Existing Rust log protections remain unchanged: private directory and file modes,
no symlink following, single-link identity checks, rotation, size limits, and the
secondary redaction layer.

### Startup marker

After the Rust diagnostic writer is configured, it will append a fixed
`desktop_diagnostics_ready` marker. This contains no dynamic data. It proves which
desktop shell initialized the log and distinguishes an empty producer from a log
path or process-identity problem.

### User-visible behavior

The UI text and retry behavior do not change. A diagnostic build still reports
the same sanitized `migration_failed` page. The user can reveal the existing
`logs` directory and return the small log file for analysis. No migration is run
against copied or uploaded user data during development; only the application on
the user's Mac operates on its existing data, with its current transactional
migration semantics.

## Verification

Tests will establish that:

1. A migration failure writes and flushes the diagnostic line before the public
   failure event.
2. Nested, cyclic, deep, malformed, and database-wrapped exceptions remain
   bounded and valid.
3. Exception messages seeded with secret-looking URLs, tokens, SQL, parameters,
   and user paths never appear in stderr.
4. A diagnostic sink failure preserves the original cleanup and public failure.
5. Rust creates the log, writes the fixed startup marker, and retains its existing
   permissions, link checks, rotation, and size bounds.
6. The complete desktop unit suite and portable macOS distribution contract suite
   pass.
7. A native Apple Silicon CI build passes bundle verification and installed-DMG
   smoke testing before its DMG is delivered.

## Follow-up

The diagnostic build is evidence gathering, not the final root-cause fix. After
the user reproduces once and returns the log, the recorded type and stable error
code will drive a single root-cause change with its own failing regression test.
