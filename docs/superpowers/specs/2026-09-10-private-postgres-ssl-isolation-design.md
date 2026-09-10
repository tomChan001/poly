# Private PostgreSQL SSL Isolation Design

## Problem

The desktop runtime connects to its bundled PostgreSQL server through a private
local socket. Its connection URL does not specify an SSL mode, so `asyncpg` can
inherit ambient PostgreSQL settings such as `PGSSLMODE=require`. The bundled
server intentionally has SSL disabled. In that environment, Alembic fails before
executing its first migration and the desktop reports a migration failure.

The supplied failing database confirms this boundary: PostgreSQL starts cleanly,
the `poly` database exists, and neither application tables nor
`alembic_version` exist. Running the same migration with inherited
`PGSSLMODE=require` reproduces the failure; explicitly disabling SSL completes
all migrations through `0009_execution_capital_settlement`.

## Design

`PostgresPaths.database_url` will add the asyncpg query parameter
`ssl=disable`. This URL is the single source used by both Alembic and the
application database layer, so the bundled private database will no longer
depend on ambient PostgreSQL SSL configuration.

The change applies only to the bundled desktop database. It does not alter
user-configured or remotely hosted database URLs.

## Validation

- Add a focused unit test proving the private database URL explicitly disables
  SSL while retaining the encoded socket path and fixed port.
- Run the desktop backend unit suite.
- Harden the macOS installed-DMG smoke environment with
  `PGSSLMODE=require`; a successful launch then proves that the packaged app
  overrides the hostile ambient setting.
- Rebuild the Apple Silicon DMG and require the installed application to reach
  its loopback listener before delivery.

## Data Safety

No migration will be run against the user's original files during development.
The supplied database remains read-only; reproduction and verification use an
isolated copy. The product fix does not reset, replace, or delete existing data.
