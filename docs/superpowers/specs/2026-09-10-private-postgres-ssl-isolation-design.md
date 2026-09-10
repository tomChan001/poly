# Private PostgreSQL SSL Isolation Design

> **Status: Superseded diagnosis (2026-09-10).** This document preserves an
> earlier SSL hypothesis. The Rust launcher uses `env_clear()`, so an exported
> `PGSSLMODE=require` does not reach the packaged Python process. The supplied
> production logs contain no SSL rejection, and the real packaged migration root
> cause remains unknown. Explicit `ssl=disable` remains a harmless defensive
> boundary for the private database, not an established production fix. Continue
> the investigation with the [Safe Desktop Migration Diagnostics design](./2026-09-10-safe-migration-diagnostics-design.md)
> and [implementation plan](../plans/2026-09-10-safe-migration-diagnostics.md).

## Problem

The desktop runtime connects to its bundled PostgreSQL server through a private
local socket. Its connection URL originally did not specify an SSL mode, so
`asyncpg` could inherit ambient PostgreSQL settings such as `PGSSLMODE=require`
in a process that retained that environment. The bundled server intentionally
has SSL disabled, making ambient SSL settings a plausible initial hypothesis.

The supplied database showed that PostgreSQL starts cleanly, the `poly` database
exists, and neither application tables nor `alembic_version` exist. That state
shows migrations did not create the schema, but it does not identify why. A
separate, out-of-package reproduction with inherited `PGSSLMODE=require` fails,
and explicitly disabling SSL lets that synthetic case migrate through
`0009_execution_capital_settlement`. Because the packaged launcher clears the
environment and the supplied logs show no SSL rejection, this reproduction does
not establish the production root cause.

## Design

`PostgresPaths.database_url` will add the asyncpg query parameter
`ssl=disable`. This URL is the single source used by both Alembic and the
application database layer, so the bundled private database will no longer
depend on ambient PostgreSQL SSL configuration.

The change applies only to the bundled desktop database. It does not alter
user-configured or remotely hosted database URLs. It is defensive isolation of a
private transport contract, not a confirmed fix for the reported packaged
migration failure.

## Validation

- Add a focused unit test proving the private database URL explicitly disables
  SSL while retaining the encoded socket path and fixed port.
- Run the desktop backend unit suite.
- Superseded: the macOS installed-DMG smoke exported `PGSSLMODE=require`, but
  `env_clear()` prevented that setting from reaching packaged Python. A
  successful launch therefore did not prove an override, and the ineffective
  smoke guard was removed.
- Rebuilding the Apple Silicon DMG and reaching its loopback listener validates
  clean-data startup only; it does not identify the reported production root
  cause. The current diagnostics work captures safe migration failure evidence.

## Data Safety

No migration will be run against the user's original files during development.
The supplied database remains read-only; reproduction and verification use an
isolated copy. The product fix does not reset, replace, or delete existing data.
