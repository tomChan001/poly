import os
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.risk_policy import PostgresRiskPolicyStore
from backend.app.services.settings import RiskPolicyInput


@pytest.mark.asyncio
async def test_postgres_risk_policy_history_survives_store_recreation() -> None:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://poly:poly@localhost:5432/poly",
    )
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    created_versions: list[UUID] = []
    table_ready = False
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS risk_policy_version (
                        version UUID PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL,
                        minimum_roi NUMERIC(38, 18) NOT NULL,
                        maximum_settlement_days INTEGER NOT NULL,
                        maximum_book_age_seconds NUMERIC(38, 18) NOT NULL,
                        per_trade_limit NUMERIC(38, 18) NOT NULL,
                        per_event_limit NUMERIC(38, 18) NOT NULL,
                        portfolio_limit NUMERIC(38, 18) NOT NULL,
                        explicit_cost NUMERIC(38, 18) NOT NULL,
                        risk_buffer NUMERIC(38, 18) NOT NULL,
                        maximum_unhedged_seconds NUMERIC(38, 18) NOT NULL,
                        maximum_unhedged_loss NUMERIC(38, 18) NOT NULL,
                        maximum_arrival_gap_seconds NUMERIC(38, 18) NOT NULL
                    )
                    """
                )
            )
        table_ready = True

        store = PostgresRiskPolicyStore(sessions)
        first = await store.create(RiskPolicyInput.defaults())
        created_versions.append(first.version)
        second = await store.create(
            replace(RiskPolicyInput.defaults(), minimum_roi=Decimal("0.07"))
        )
        created_versions.append(second.version)

        recreated = PostgresRiskPolicyStore(sessions)
        assert await recreated.get(first.version) == first
        assert await recreated.get(second.version) == second
        assert await recreated.initialize() == second
        assert recreated.current == second
    finally:
        if table_ready:
            async with engine.begin() as connection:
                for version in created_versions:
                    await connection.execute(
                        text("DELETE FROM risk_policy_version WHERE version = :version"),
                        {"version": version},
                    )
        await engine.dispose()
