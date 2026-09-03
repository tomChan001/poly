# Durable Risk Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the display-only/in-memory risk configuration with immutable PostgreSQL-backed policy versions that the API, runtime, and frontend use immediately and retain across restarts.

**Architecture:** Introduce an async `RiskPolicyStore` contract with in-memory and PostgreSQL implementations. The runtime container initializes the latest persisted policy before polling starts, while the Risk Settings page loads and saves the same active policy through typed API helpers.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, PostgreSQL, Alembic, pytest, React 19, TypeScript, Vitest, Testing Library.

**Execution order:** Complete this plan before `2026-09-03-unified-real-ordering-control.md`, because the live runtime plan consumes the async durable policy contract defined here.

---

### Task 1: Make the risk-policy service contract async and version-aware

**Files:**
- Modify: `backend/app/services/settings.py`
- Modify: `backend/app/container.py`
- Modify: `backend/app/api/routes/settings.py`
- Modify: `backend/tests/unit/services/test_settings.py`
- Modify: `backend/tests/integration/services/test_live_runtime.py`

- [ ] **Step 1: Write the failing async in-memory store tests**

Replace the current unit test with tests that prove initialization, immutable versions, and creation timestamps:

```python
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.app.services.settings import InMemoryRiskPolicyStore, RiskPolicyInput


@pytest.mark.asyncio
async def test_risk_policy_updates_create_immutable_versions() -> None:
    store = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())
    first = await store.initialize()
    second_input = RiskPolicyInput.defaults()
    second_input.minimum_roi = Decimal("0.05")

    second = await store.create(second_input)

    assert first.version != second.version
    assert first.minimum_roi == Decimal("0.03")
    assert second.minimum_roi == Decimal("0.05")
    assert await store.get(first.version) is first
    assert second.created_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_initialize_returns_the_existing_default_without_new_version() -> None:
    store = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())

    first = await store.initialize()
    second = await store.initialize()

    assert first is second
    assert store.current is first
```

- [ ] **Step 2: Run the tests and verify the synchronous API fails**

Run: `uv run pytest backend/tests/unit/services/test_settings.py -v`

Expected: FAIL because `InMemoryRiskPolicyStore` does not accept an initial policy, its methods are synchronous, and `RiskPolicy` has no `created_at`.

- [ ] **Step 3: Add the async store protocol and update the in-memory implementation**

Update `backend/app/services/settings.py` so all store implementations share this interface and snapshot behavior:

```python
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4


@dataclass(slots=True)
class RiskPolicyInput:
    minimum_roi: Decimal
    maximum_settlement_days: int
    maximum_book_age_seconds: Decimal
    per_trade_limit: Decimal
    per_event_limit: Decimal
    portfolio_limit: Decimal
    explicit_cost: Decimal
    risk_buffer: Decimal
    maximum_unhedged_seconds: Decimal
    maximum_unhedged_loss: Decimal
    maximum_arrival_gap_seconds: Decimal = Decimal("0.5")

    @classmethod
    def defaults(cls) -> "RiskPolicyInput":
        return cls(
            minimum_roi=Decimal("0.03"),
            maximum_settlement_days=30,
            maximum_book_age_seconds=Decimal(2),
            per_trade_limit=Decimal(10),
            per_event_limit=Decimal(25),
            portfolio_limit=Decimal(100),
            explicit_cost=Decimal(0),
            risk_buffer=Decimal("0.25"),
            maximum_unhedged_seconds=Decimal(2),
            maximum_unhedged_loss=Decimal(2),
            maximum_arrival_gap_seconds=Decimal("0.5"),
        )


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    version: UUID
    created_at: datetime
    minimum_roi: Decimal
    maximum_settlement_days: int
    maximum_book_age_seconds: Decimal
    per_trade_limit: Decimal
    per_event_limit: Decimal
    portfolio_limit: Decimal
    explicit_cost: Decimal
    risk_buffer: Decimal
    maximum_unhedged_seconds: Decimal
    maximum_unhedged_loss: Decimal
    maximum_arrival_gap_seconds: Decimal = Decimal("0.5")


class RiskPolicyStore(Protocol):
    current: RiskPolicy | None

    async def initialize(self) -> RiskPolicy:
        raise NotImplementedError

    async def create(self, value: RiskPolicyInput) -> RiskPolicy:
        raise NotImplementedError

    async def get(self, version: UUID) -> RiskPolicy:
        raise NotImplementedError


class InMemoryRiskPolicyStore:
    def __init__(self, initial: RiskPolicyInput | None = None) -> None:
        self._versions: dict[UUID, RiskPolicy] = {}
        self.current: RiskPolicy | None = None
        if initial is not None:
            self.current = self._create(initial)

    async def initialize(self) -> RiskPolicy:
        if self.current is None:
            self.current = self._create(RiskPolicyInput.defaults())
        return self.current

    async def create(self, value: RiskPolicyInput) -> RiskPolicy:
        self.current = self._create(value)
        return self.current

    async def get(self, version: UUID) -> RiskPolicy:
        return self._versions[version]

    def _create(self, value: RiskPolicyInput) -> RiskPolicy:
        snapshot = replace(value)
        policy = RiskPolicy(
            version=uuid4(),
            created_at=datetime.now(UTC),
            **asdict(snapshot),
        )
        self._versions[policy.version] = policy
        return policy
```

Update synchronous construction sites so they seed through the constructor rather than calling the now-async method:

```python
# backend/app/container.py
self.risk_policies = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())
```

Replace each two-line test setup in `test_live_runtime.py` with:

```python
risk = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())
```

Make the settings update route async immediately so the repository remains green after this commit:

```python
@router.put("/risk")
async def update_risk_policy(
    payload: RiskPolicyRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
    _principal: Annotated[Principal, Depends(require_role(Role.OPERATOR))],
) -> RiskPolicy:
    policy_input = RiskPolicyInput(**payload.model_dump())
    return await container.risk_policies.create(policy_input)
```

- [ ] **Step 4: Run the focused service tests**

Run: `uv run pytest backend/tests/unit/services/test_settings.py backend/tests/integration/api/test_settings.py backend/tests/integration/services/test_live_runtime.py -v`

Expected: all selected tests PASS.

- [ ] **Step 5: Commit the service contract**

```bash
git add backend/app/services/settings.py backend/app/container.py backend/app/api/routes/settings.py backend/tests/unit/services/test_settings.py backend/tests/integration/services/test_live_runtime.py
git commit -m "refactor: define async risk policy store"
```

### Task 2: Add the immutable PostgreSQL policy table and repository

**Files:**
- Create: `backend/app/db/risk_policy.py`
- Create: `migrations/versions/0007_risk_policy_versions.py`
- Create: `backend/tests/integration/db/test_risk_policy_repository.py`
- Modify: `backend/app/db/tables.py`
- Modify: `migrations/versions/0001_initial_schema.py`
- Modify: `backend/tests/integration/db/test_initial_schema.py`
- Modify: `backend/tests/integration/db/test_initial_migration.py`
- Modify: `backend/tests/integration/db/test_postgres_runtime.py`

- [ ] **Step 1: Write repository persistence tests**

Create `backend/tests/integration/db/test_risk_policy_repository.py` following the repository's existing PostgreSQL test convention:

```python
import os
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.risk_policy import PostgresRiskPolicyStore
from backend.app.services.settings import RiskPolicyInput


@pytest.mark.asyncio
async def test_risk_policy_versions_survive_store_recreation() -> None:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://poly:poly@localhost:5432/poly",
    )
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    created_versions = []
    try:
        first_store = PostgresRiskPolicyStore(sessions)
        default = await first_store.create(RiskPolicyInput.defaults())
        created_versions.append(default.version)
        update = RiskPolicyInput.defaults()
        update.minimum_roi = Decimal("0.07")
        saved = await first_store.create(update)
        created_versions.append(saved.version)

        restored_store = PostgresRiskPolicyStore(sessions)
        restored = await restored_store.initialize()

        assert default.version != saved.version
        assert restored == saved
        assert await restored_store.get(default.version) == default
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM risk_policy_version WHERE version = ANY(:versions)"),
                {"versions": created_versions},
            )
        await engine.dispose()
```

- [ ] **Step 2: Add schema assertions before implementing the table**

Add `risk_policy_version` to the expected set in `test_initial_schema.py` and to `EXPECTED_TABLES` in `0001_initial_schema.py`; this repository's initial migration creates current metadata and later projection migrations therefore use `if_not_exists`. Extend `test_postgres_runtime.py` to assert that the migrated table exists and contains these columns:

```python
assert "risk_policy_version" in table_names
risk_columns = set(
    await connection.fetchval(
        """
        SELECT array_agg(column_name)
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'risk_policy_version'
        """
    )
)
assert risk_columns == {
    "version", "created_at", "minimum_roi", "maximum_settlement_days",
    "maximum_book_age_seconds", "per_trade_limit", "per_event_limit",
    "portfolio_limit", "explicit_cost", "risk_buffer",
    "maximum_unhedged_seconds", "maximum_unhedged_loss",
    "maximum_arrival_gap_seconds",
}
```

Inside the isolated migrated database in `test_postgres_runtime.py`, initialize and recreate the store to prove startup seeding and latest-version loading:

```python
risk_store = PostgresRiskPolicyStore(sessions)
seeded = await risk_store.initialize()
updated_input = RiskPolicyInput.defaults()
updated_input.minimum_roi = Decimal("0.06")
updated = await risk_store.create(updated_input)
restarted_store = PostgresRiskPolicyStore(sessions)

assert seeded.minimum_roi == Decimal("0.03")
assert updated.version != seeded.version
assert await restarted_store.initialize() == updated
```

- [ ] **Step 3: Run the schema and repository tests and verify they fail**

Run: `uv run pytest backend/tests/integration/db/test_initial_schema.py backend/tests/integration/db/test_initial_migration.py backend/tests/integration/db/test_risk_policy_repository.py -v`

Expected: FAIL because the model, migration, and repository do not exist.

- [ ] **Step 4: Define the SQLAlchemy model and Alembic migration**

Add this model to `backend/app/db/tables.py`:

```python
class RiskPolicyVersion(Base):
    __tablename__ = "risk_policy_version"

    version: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    minimum_roi: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    maximum_settlement_days: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_book_age_seconds: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    per_trade_limit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    per_event_limit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    portfolio_limit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    explicit_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    risk_buffer: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    maximum_unhedged_seconds: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    maximum_unhedged_loss: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    maximum_arrival_gap_seconds: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
```

Create `migrations/versions/0007_risk_policy_versions.py`:

```python
"""Persist immutable risk policy versions.

Revision ID: 0007_risk_policy_versions
Revises: 0006_fixed_oddpool_endpoint
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_risk_policy_versions"
down_revision: str | None = "0006_fixed_oddpool_endpoint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "risk_policy_version",
        sa.Column("version", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("minimum_roi", sa.Numeric(38, 18), nullable=False),
        sa.Column("maximum_settlement_days", sa.Integer(), nullable=False),
        sa.Column("maximum_book_age_seconds", sa.Numeric(38, 18), nullable=False),
        sa.Column("per_trade_limit", sa.Numeric(38, 18), nullable=False),
        sa.Column("per_event_limit", sa.Numeric(38, 18), nullable=False),
        sa.Column("portfolio_limit", sa.Numeric(38, 18), nullable=False),
        sa.Column("explicit_cost", sa.Numeric(38, 18), nullable=False),
        sa.Column("risk_buffer", sa.Numeric(38, 18), nullable=False),
        sa.Column("maximum_unhedged_seconds", sa.Numeric(38, 18), nullable=False),
        sa.Column("maximum_unhedged_loss", sa.Numeric(38, 18), nullable=False),
        sa.Column("maximum_arrival_gap_seconds", sa.Numeric(38, 18), nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("risk_policy_version")
```

- [ ] **Step 5: Implement the PostgreSQL store**

Create `backend/app/db/risk_policy.py`:

```python
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.services.settings import RiskPolicy, RiskPolicyInput


class PostgresRiskPolicyStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self.current: RiskPolicy | None = None

    async def initialize(self) -> RiskPolicy:
        async with self._sessions() as session:
            result = await session.execute(
                text("SELECT * FROM risk_policy_version ORDER BY created_at DESC LIMIT 1")
            )
            row = result.first()
        if row is None:
            return await self.create(RiskPolicyInput.defaults())
        self.current = self._policy(row._mapping)
        return self.current

    async def create(self, value: RiskPolicyInput) -> RiskPolicy:
        policy = RiskPolicy(
            version=uuid4(),
            created_at=datetime.now(UTC),
            **asdict(value),
        )
        payload = asdict(policy)
        async with self._sessions.begin() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO risk_policy_version (
                        version, created_at, minimum_roi, maximum_settlement_days,
                        maximum_book_age_seconds, per_trade_limit, per_event_limit,
                        portfolio_limit, explicit_cost, risk_buffer,
                        maximum_unhedged_seconds, maximum_unhedged_loss,
                        maximum_arrival_gap_seconds
                    ) VALUES (
                        :version, :created_at, :minimum_roi, :maximum_settlement_days,
                        :maximum_book_age_seconds, :per_trade_limit, :per_event_limit,
                        :portfolio_limit, :explicit_cost, :risk_buffer,
                        :maximum_unhedged_seconds, :maximum_unhedged_loss,
                        :maximum_arrival_gap_seconds
                    )
                    """
                ),
                payload,
            )
        self.current = policy
        return policy

    async def get(self, version: UUID) -> RiskPolicy:
        async with self._sessions() as session:
            result = await session.execute(
                text("SELECT * FROM risk_policy_version WHERE version = :version"),
                {"version": version},
            )
            row = result.first()
        if row is None:
            raise KeyError(version)
        return self._policy(row._mapping)

    @staticmethod
    def _policy(row: RowMapping) -> RiskPolicy:
        return RiskPolicy(
            version=cast(UUID, row["version"]),
            created_at=cast(datetime, row["created_at"]),
            minimum_roi=cast(Decimal, row["minimum_roi"]),
            maximum_settlement_days=cast(int, row["maximum_settlement_days"]),
            maximum_book_age_seconds=cast(Decimal, row["maximum_book_age_seconds"]),
            per_trade_limit=cast(Decimal, row["per_trade_limit"]),
            per_event_limit=cast(Decimal, row["per_event_limit"]),
            portfolio_limit=cast(Decimal, row["portfolio_limit"]),
            explicit_cost=cast(Decimal, row["explicit_cost"]),
            risk_buffer=cast(Decimal, row["risk_buffer"]),
            maximum_unhedged_seconds=cast(Decimal, row["maximum_unhedged_seconds"]),
            maximum_unhedged_loss=cast(Decimal, row["maximum_unhedged_loss"]),
            maximum_arrival_gap_seconds=cast(Decimal, row["maximum_arrival_gap_seconds"]),
        )
```

- [ ] **Step 6: Run focused backend persistence tests**

Run: `uv run pytest backend/tests/unit/services/test_settings.py backend/tests/integration/db/test_initial_schema.py backend/tests/integration/db/test_initial_migration.py backend/tests/integration/db/test_risk_policy_repository.py -v`

Expected: all selected tests PASS. If PostgreSQL is unavailable, run the unit/schema tests locally and record the repository test as environment-blocked for the final verification environment.

- [ ] **Step 7: Commit the persistence layer**

```bash
git add backend/app/db/risk_policy.py backend/app/db/tables.py migrations/versions/0001_initial_schema.py migrations/versions/0007_risk_policy_versions.py backend/tests/integration/db/test_risk_policy_repository.py backend/tests/integration/db/test_initial_schema.py backend/tests/integration/db/test_initial_migration.py backend/tests/integration/db/test_postgres_runtime.py
git commit -m "feat: persist immutable risk policies"
```

### Task 3: Wire the durable store into startup, API, and runtime

**Files:**
- Modify: `backend/app/container.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/services/live_runtime.py`
- Modify: `backend/tests/integration/api/test_settings.py`
- Modify: `backend/tests/unit/test_live_runtime_loop.py`

- [ ] **Step 1: Add failing API and startup tests**

Update API calls to expect async creation and add a startup initialization spy:

```python
def valid_policy(minimum_roi: str = "0.05") -> dict[str, object]:
    return {
        "minimum_roi": minimum_roi,
        "maximum_settlement_days": 20,
        "maximum_book_age_seconds": "1.5",
        "per_trade_limit": "10",
        "per_event_limit": "25",
        "portfolio_limit": "100",
        "explicit_cost": "0",
        "risk_buffer": "0.25",
        "maximum_unhedged_seconds": "2",
        "maximum_unhedged_loss": "2",
        "maximum_arrival_gap_seconds": "0.5",
    }


@pytest.mark.asyncio
async def test_settings_update_becomes_the_active_policy() -> None:
    container = ApplicationContainer()
    original_version = str(container.risk_policies.current.version)
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "operator", frozenset({Role.OPERATOR})
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/api/settings/risk", json=valid_policy("0.05"))
        current = await client.get("/api/settings/risk")

    assert response.status_code == 200
    assert current.json() == response.json()
    assert current.json()["version"] != original_version
    assert current.json()["created_at"] is not None
```

- [ ] **Step 2: Run the focused API tests and verify they fail**

Run: `uv run pytest backend/tests/integration/api/test_settings.py backend/tests/unit/test_live_runtime_loop.py -v`

Expected: FAIL because routes still call `create()` synchronously and runtime startup does not initialize the durable store.

- [ ] **Step 3: Wire the store and await initialization**

Make these exact structural changes:

```python
# backend/app/container.py
from backend.app.db.risk_policy import PostgresRiskPolicyStore
from backend.app.services.settings import InMemoryRiskPolicyStore, RiskPolicyInput

# ApplicationContainer.runtime, after sessions are created
container.risk_policies = PostgresRiskPolicyStore(sessions)
```

```python
# backend/app/main.py lifespan, before starting _live_runtime_loop
await application_container.system_control.load_async()
await application_container.risk_policies.initialize()
```

Change `LiveRuntimeService.risk_policies` from the concrete `InMemoryRiskPolicyStore` annotation to `RiskPolicyStore`; runtime behavior still reads the already initialized `current` snapshot once per cycle.

- [ ] **Step 4: Run API, startup, and live-runtime tests**

Run: `uv run pytest backend/tests/integration/api/test_settings.py backend/tests/unit/test_live_runtime_loop.py backend/tests/integration/services/test_live_runtime.py -v`

Expected: all selected tests PASS.

- [ ] **Step 5: Commit runtime wiring**

```bash
git add backend/app/container.py backend/app/main.py backend/app/services/live_runtime.py backend/tests/integration/api/test_settings.py backend/tests/unit/test_live_runtime_loop.py
git commit -m "feat: load active risk policy at startup"
```

### Task 4: Connect the Risk Settings page to the backend

**Files:**
- Create: `frontend/src/types/risk.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/pages/RiskSettingsPage.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/App.css`

- [ ] **Step 1: Write failing frontend load/save tests**

Add a test that routes `/api/settings/risk`, opens 风控, verifies backend values, edits ROI, and verifies the PUT body contains a ratio:

```tsx
const riskPolicy = {
  version: 'risk-v1',
  created_at: '2026-09-03T02:00:00Z',
  minimum_roi: '0.03',
  maximum_settlement_days: 30,
  maximum_book_age_seconds: '2',
  per_trade_limit: '10',
  per_event_limit: '25',
  portfolio_limit: '100',
  explicit_cost: '0',
  risk_buffer: '0.25',
  maximum_unhedged_seconds: '2',
  maximum_unhedged_loss: '2',
  maximum_arrival_gap_seconds: '0.5',
}

test('loads and saves the active risk policy', async () => {
  let savedBody: Record<string, unknown> | null = null
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/settings/risk')) {
      if (init?.method === 'PUT') {
        savedBody = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve({
          ok: true,
          json: async () => ({ ...riskPolicy, ...savedBody, version: 'risk-v2' }),
        })
      }
      return Promise.resolve({ ok: true, json: async () => riskPolicy })
    }
    if (url.includes('/api/runtime')) {
      return Promise.resolve({ ok: true, json: async () => runtimeStatus })
    }
    if (url.includes('/health')) {
      return Promise.resolve({
        ok: true,
        json: async () => ({
          status: 'ok',
          trading_mode: 'limited_auto',
          opening_enabled: false,
          reason: 'safe default',
        }),
      })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  }))

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '风控' }))

  expect(await screen.findByLabelText('最低保守 ROI')).toHaveValue(3)
  expect(screen.getByText(`策略版本 ${riskPolicy.version}`)).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('最低保守 ROI'), { target: { value: '5' } })
  fireEvent.click(screen.getByRole('button', { name: '保存策略' }))

  await waitFor(() => expect(savedBody?.minimum_roi).toBe('0.05'))
  expect(await screen.findByText('已生成新策略版本')).toBeInTheDocument()
})
```

- [ ] **Step 2: Run the frontend test and verify it fails**

Run: `npm --prefix frontend test -- frontend/src/App.test.tsx`

Expected: FAIL because the page does not call the risk API and uses uncontrolled default values.

- [ ] **Step 3: Add typed API helpers**

Create `frontend/src/types/risk.ts`:

```ts
export interface RiskPolicy {
  version: string
  created_at: string
  minimum_roi: string
  maximum_settlement_days: number
  maximum_book_age_seconds: string
  per_trade_limit: string
  per_event_limit: string
  portfolio_limit: string
  explicit_cost: string
  risk_buffer: string
  maximum_unhedged_seconds: string
  maximum_unhedged_loss: string
  maximum_arrival_gap_seconds: string
}

export type RiskPolicyUpdate = Omit<RiskPolicy, 'version' | 'created_at'>
```

Add to `frontend/src/api/client.ts`:

```ts
export function getRiskPolicy(): Promise<RiskPolicy> {
  return apiRequest<RiskPolicy>('/api/settings/risk')
}

export function saveRiskPolicy(value: RiskPolicyUpdate): Promise<RiskPolicy> {
  return apiRequest<RiskPolicy>('/api/settings/risk', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(value),
  })
}
```

- [ ] **Step 4: Replace the display-only form with controlled backend state**

Implement `RiskSettingsPage` with `loading`, `saving`, `error`, and `policy` state. Convert ratios only at the UI boundary:

```ts
const percent = (ratio: string) => String(Number(ratio) * 100)
const ratio = (value: string) => String(Number(value) / 100)

const toForm = (policy: RiskPolicy): RiskForm => ({
  minimumRoiPercent: percent(policy.minimum_roi),
  maximumSettlementDays: String(policy.maximum_settlement_days),
  maximumBookAgeSeconds: policy.maximum_book_age_seconds,
  perTradeLimit: policy.per_trade_limit,
  perEventLimit: policy.per_event_limit,
  portfolioLimit: policy.portfolio_limit,
  explicitCost: policy.explicit_cost,
  riskBuffer: policy.risk_buffer,
  maximumUnhedgedSeconds: policy.maximum_unhedged_seconds,
  maximumUnhedgedLoss: policy.maximum_unhedged_loss,
  maximumArrivalGapSeconds: policy.maximum_arrival_gap_seconds,
})

const toUpdate = (form: RiskForm): RiskPolicyUpdate => ({
  minimum_roi: ratio(form.minimumRoiPercent),
  maximum_settlement_days: Number(form.maximumSettlementDays),
  maximum_book_age_seconds: form.maximumBookAgeSeconds,
  per_trade_limit: form.perTradeLimit,
  per_event_limit: form.perEventLimit,
  portfolio_limit: form.portfolioLimit,
  explicit_cost: form.explicitCost,
  risk_buffer: form.riskBuffer,
  maximum_unhedged_seconds: form.maximumUnhedgedSeconds,
  maximum_unhedged_loss: form.maximumUnhedgedLoss,
  maximum_arrival_gap_seconds: form.maximumArrivalGapSeconds,
})
```

Use `aria-label` on every input, disable the form while loading/saving, keep the edited values on a failed save, and show `策略版本 {version}` plus the formatted `created_at` after a successful save.

- [ ] **Step 5: Run frontend unit tests and build**

Run: `npm --prefix frontend test -- frontend/src/App.test.tsx`

Expected: all App tests PASS.

Run: `npm --prefix frontend run build`

Expected: TypeScript and Vite build PASS.

- [ ] **Step 6: Commit the frontend risk integration**

```bash
git add frontend/src/types/risk.ts frontend/src/api/client.ts frontend/src/pages/RiskSettingsPage.tsx frontend/src/App.test.tsx frontend/src/App.css
git commit -m "feat: connect risk settings to persisted policy"
```

### Task 5: Run complete risk-policy verification

**Files:**
- Modify if required by verified behavior: `README.md`

- [ ] **Step 1: Document durable risk behavior**

Add a short README statement that risk saves create immutable versions, become active on the next polling cycle, and survive process restart.

- [ ] **Step 2: Run backend quality checks**

Run: `uv run ruff check backend/app backend/tests migrations`

Expected: PASS.

Run: `uv run mypy backend/app`

Expected: PASS.

Run: `uv run pytest backend/tests/unit backend/tests/integration/api/test_settings.py backend/tests/integration/services/test_live_runtime.py -q`

Expected: PASS.

- [ ] **Step 3: Run migration-backed PostgreSQL verification**

Run: `uv run pytest backend/tests/integration/db/test_postgres_runtime.py backend/tests/integration/db/test_risk_policy_repository.py -v`

Expected: PASS with PostgreSQL available and `TEST_POSTGRES_ADMIN_URL` configured for the migration test.

- [ ] **Step 4: Run frontend verification**

Run: `npm --prefix frontend run lint`

Expected: PASS.

Run: `npm --prefix frontend test`

Expected: PASS.

Run: `npm --prefix frontend run build`

Expected: PASS.

- [ ] **Step 5: Commit documentation or verification fixes**

```bash
git add README.md
git commit -m "docs: explain durable risk policy versions"
```
