# Integration Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local operator web menu for configuring Oddpool, Kalshi, and Polymarket integrations without ever returning stored secrets to the browser.

**Architecture:** PostgreSQL stores versioned non-secret integration settings and audit metadata. A `SecretStore` port isolates sensitive values; the runtime implementation uses the operating-system credential store, while tests use an in-memory implementation. The API exposes read, update, secret-delete, and connection-test operations; secret fields are write-only and responses contain only status and a masked fingerprint.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy async, PostgreSQL 16, keyring, React, TypeScript, Vitest, Playwright.

---

### Task 1: Durable configuration model and migration

**Files:**
- Modify: `backend/app/db/tables.py`
- Create: `migrations/versions/0002_integration_configuration.py`
- Test: `backend/tests/integration/db/test_postgres_runtime.py`

- [ ] Add a failing real-PostgreSQL assertion for the `integration_config` table, including provider, enabled state, JSON configuration, version, updater, and update time.
- [ ] Run the database test with `TEST_POSTGRES_ADMIN_URL` and confirm it fails because the table is absent.
- [ ] Add the SQLAlchemy table and Alembic migration without any secret-value column.
- [ ] Re-run upgrade, downgrade, and runtime migration tests.

### Task 2: Secret-safe configuration service and API

**Files:**
- Create: `backend/app/services/integration_config.py`
- Create: `backend/app/db/integration_config.py`
- Create: `backend/app/core/secrets.py`
- Create: `backend/app/api/routes/integrations.py`
- Modify: `backend/app/container.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/core/config.py`
- Test: `backend/tests/unit/services/test_integration_config.py`
- Test: `backend/tests/integration/api/test_integrations.py`

- [ ] Write failing service tests proving secrets are write-only, masked fingerprints are stable, unchanged secret inputs preserve existing values, and delete removes credentials.
- [ ] Write failing API tests proving only operators can mutate/test integrations and GET responses never contain raw secrets.
- [ ] Define provider-specific Pydantic configuration models with HTTPS URL validation, explicit sandbox/production environment, and allowed secret field names.
- [ ] Implement PostgreSQL upsert/versioning plus a `SecretStore` protocol and OS-keyring runtime adapter.
- [ ] Add connection probes that use the configured endpoint and credential without logging request headers or bodies.
- [ ] Wire the runtime container to PostgreSQL/keyring while retaining in-memory stores for injected test containers.

### Task 3: Web configuration menu

**Files:**
- Create: `frontend/src/types/integration.ts`
- Create: `frontend/src/pages/IntegrationSettingsPage.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.css`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/tests/opportunities.spec.ts`

- [ ] Write a failing component test for navigation, provider status, write-only secret fields, save, delete, and connection-test states.
- [ ] Add typed API methods and a dedicated navigation item using a Lucide plug icon.
- [ ] Build compact provider sections with endpoint/environment controls, masked credential status, password inputs, save/test/delete actions, pending/error/success states, and mobile-safe layout.
- [ ] Update Playwright routes and verify desktop/mobile controls do not overlap.

### Task 4: End-to-end verification

**Files:**
- Modify: `.env.example`
- Modify: `README.md`

- [ ] Document the OS credential-store boundary, operator permissions, service-account identity requirement, and bootstrap database settings.
- [ ] Apply migrations to the real local PostgreSQL instance.
- [ ] Run backend tests with real database integration, coverage, Ruff, and mypy.
- [ ] Run frontend unit tests, build, lint, and Chromium desktop/mobile Playwright tests.
- [ ] Start the updated API and frontend and verify the configuration page is reachable without exposing any secret value.
