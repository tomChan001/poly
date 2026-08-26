import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.executions import PostgresExecutionStore
from backend.app.domain.enums import ExecutionState, Venue
from backend.app.services.execution import (
    ExecutionEvidence,
    ExecutionRecord,
    FillReport,
    OrderStatus,
    OrderSubmissionResult,
    StateTransition,
)


@pytest.mark.asyncio
async def test_postgres_execution_history_survives_repository_recreation() -> None:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://poly:poly@localhost:5432/poly",
    )
    engine = create_async_engine(database_url)
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
