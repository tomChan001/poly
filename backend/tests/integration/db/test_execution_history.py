import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.executions import PostgresExecutionStore, _record, _snapshot
from backend.app.domain.enums import ExecutionState, Venue
from backend.app.services.execution import (
    ExecutionEvidence,
    ExecutionRecord,
    FillReport,
    OrderStatus,
    OrderSubmissionResult,
    StateTransition,
)


async def require_postgres(engine) -> None:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (OSError, asyncpg.PostgresError) as error:
        await engine.dispose()
        pytest.skip(f"PostgreSQL is unavailable: {error}")


def test_execution_snapshot_defaults_missing_capital_settlement_to_false() -> None:
    restored = _record(
        {
            "correlation_id": "legacy-execution",
            "state": "paired",
            "requested_quantity": "10",
            "matched_quantity": "10",
            "unhedged_quantity": "0",
            "evidence": None,
            "legs": {},
            "transitions": [],
        }
    )

    assert restored.capital_settled is False
    assert _snapshot(restored)["capital_settled"] is False

    snapshot = _snapshot(
        ExecutionRecord(
            correlation_id="column-wins",
            state=ExecutionState.PAIRED,
            requested_quantity=Decimal(10),
            capital_settled=True,
        )
    )
    assert _record(snapshot, capital_settled=False).capital_settled is False


@pytest.mark.asyncio
async def test_postgres_recovery_candidate_query_targets_unsettled_records() -> None:
    class RecordingSession:
        statement = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def execute(self, statement):
            self.statement = statement
            return []

    session = RecordingSession()
    store = PostgresExecutionStore(lambda: session)

    assert await store.list_recovery_candidates() == []
    assert session.statement is not None
    sql = session.statement.text
    assert "state = 'submitted'" in sql
    assert "capital_settled = FALSE" in sql
    assert "snapshot ->>" not in sql
    assert "ORDER BY occurred_at DESC" in sql


@pytest.mark.asyncio
async def test_postgres_save_persists_capital_settlement_column() -> None:
    class RecordingSession:
        statement = None
        parameters = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def execute(self, statement, parameters):
            self.statement = statement
            self.parameters = parameters

    class RecordingSessions:
        def __init__(self, session) -> None:
            self._session = session

        def begin(self):
            return self._session

    session = RecordingSession()
    store = PostgresExecutionStore(RecordingSessions(session))

    await store.save(
        ExecutionRecord(
            correlation_id="settled-execution",
            state=ExecutionState.PAIRED,
            requested_quantity=Decimal(10),
            capital_settled=True,
        )
    )

    assert session.statement is not None
    assert "capital_settled" in session.statement.text
    assert session.parameters["capital_settled"] is True


@pytest.mark.asyncio
async def test_postgres_claim_submission_inserts_snapshot_without_overwriting() -> None:
    class Result:
        def scalar_one_or_none(self) -> str:
            return "claimed-execution"

    class RecordingSession:
        statement = None
        parameters = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def execute(self, statement, parameters):
            self.statement = statement
            self.parameters = parameters
            return Result()

    class RecordingSessions:
        def __init__(self, session) -> None:
            self._session = session

        def begin(self):
            return self._session

    session = RecordingSession()
    record = ExecutionRecord(
        correlation_id="claimed-execution",
        state=ExecutionState.SUBMITTED,
        requested_quantity=Decimal(10),
        capital_settled=True,
    )

    claimed = await PostgresExecutionStore(RecordingSessions(session)).claim_submission(
        record
    )

    assert claimed is True
    assert session.statement is not None
    assert "ON CONFLICT (correlation_id) DO NOTHING" in session.statement.text
    assert "RETURNING correlation_id" in session.statement.text
    assert "DO UPDATE" not in session.statement.text
    assert session.parameters["state"] == "submitted"
    assert session.parameters["capital_settled"] is True
    assert '"requested_quantity": "10"' in session.parameters["snapshot"]
    assert '"capital_settled": true' in session.parameters["snapshot"]


@pytest.mark.asyncio
async def test_postgres_concurrent_submission_claims_only_one_store() -> None:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://poly:poly@localhost:5432/poly",
    )
    engine = create_async_engine(database_url)
    await require_postgres(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    correlation_id = f"concurrent-claim-{uuid4()}"
    record = ExecutionRecord(
        correlation_id=correlation_id,
        state=ExecutionState.SUBMITTED,
        requested_quantity=Decimal(10),
        capital_settled=False,
    )

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS execution_record (
                        correlation_id VARCHAR(64) PRIMARY KEY,
                        occurred_at TIMESTAMPTZ NOT NULL,
                        state VARCHAR(32) NOT NULL,
                        capital_settled BOOLEAN NOT NULL DEFAULT FALSE,
                        snapshot JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
            )
        first, second = await asyncio.gather(
            PostgresExecutionStore(sessions).claim_submission(record),
            PostgresExecutionStore(sessions).claim_submission(record),
        )

        assert sorted((first, second)) == [False, True]
        persisted = await PostgresExecutionStore(sessions).get(correlation_id)
        assert persisted.state is ExecutionState.SUBMITTED
        assert persisted.capital_settled is False
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM execution_record WHERE correlation_id = :correlation_id"),
                {"correlation_id": correlation_id},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_execution_history_survives_repository_recreation() -> None:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://poly:poly@localhost:5432/poly",
    )
    engine = create_async_engine(database_url)
    await require_postgres(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    correlation_id = f"history-{uuid4()}"
    occurred_at = datetime(2026, 8, 19, 1, 2, 3, tzinfo=UTC)
    record = ExecutionRecord(
        correlation_id=correlation_id,
        state=ExecutionState.PAIRED,
        requested_quantity=Decimal("10.5"),
        matched_quantity=Decimal("10.5"),
        evidence=ExecutionEvidence(
            quote_evaluation_id="quote-history",
            rule_versions=("k-rule", "p-rule"),
            book_sequences=("k-book", "p-book"),
            balance_versions=("k-balance", "p-balance"),
            risk_policy_version="risk-1",
            capital_reservation_id="reserve-history",
            quantity=Decimal("10.5"),
            kalshi_market_id="K",
            polymarket_market_id="P",
            kalshi_outcome="NO",
            polymarket_outcome="YES",
            kalshi_limit_price=Decimal("0.70"),
            polymarket_limit_price=Decimal("0.20"),
            conservative_roi=Decimal("0.08"),
            minimum_roi=Decimal("0.03"),
            estimated_fees=(Decimal("0.03"), Decimal("0.01")),
        ),
        legs={
            Venue.KALSHI: OrderSubmissionResult(
                f"{correlation_id}-kalshi",
                OrderStatus.FILLED,
                (
                    FillReport(
                        "k-fill", Decimal("10.5"), Decimal("0.70"), Decimal("0.03")
                    ),
                ),
            ),
            Venue.POLYMARKET: OrderSubmissionResult(
                f"{correlation_id}-polymarket",
                OrderStatus.FILLED,
                (
                    FillReport(
                        "p-fill", Decimal("10.5"), Decimal("0.20"), Decimal("0.01")
                    ),
                ),
            ),
        },
        transitions=[
            StateTransition(
                ExecutionState.PRECHECKED, ExecutionState.SUBMITTED, occurred_at
            ),
            StateTransition(
                ExecutionState.SUBMITTED, ExecutionState.PAIRED, occurred_at
            ),
        ],
    )

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS execution_record (
                        correlation_id VARCHAR(64) PRIMARY KEY,
                        occurred_at TIMESTAMPTZ NOT NULL,
                        state VARCHAR(32) NOT NULL,
                        capital_settled BOOLEAN NOT NULL DEFAULT FALSE,
                        snapshot JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
            )
        await PostgresExecutionStore(sessions).save(record)
        recreated = PostgresExecutionStore(sessions)

        restored = await recreated.get(correlation_id)
        listed = await recreated.list()

        assert restored == record
        assert correlation_id in {item.correlation_id for item in listed}
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM execution_record WHERE correlation_id = :correlation_id"
                ),
                {"correlation_id": correlation_id},
            )
        await engine.dispose()
