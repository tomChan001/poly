import asyncio
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


@pytest.mark.asyncio
async def test_concurrent_postgres_risk_policy_initialize_seeds_one_default_version() -> None:
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://poly:poly@localhost:5432/poly",
    )
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    created_versions: set[UUID] = set()
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
            existing_versions = set(
                (
                    await connection.execute(
                        text("SELECT version FROM risk_policy_version")
                    )
                ).scalars()
            )
        table_ready = True

        first_store = PostgresRiskPolicyStore(sessions)
        second_store = PostgresRiskPolicyStore(sessions)
        first, second = await asyncio.gather(
            first_store.initialize(), second_store.initialize()
        )
        created_versions = {first.version, second.version} - existing_versions

        assert len(created_versions) == 1
        assert first.version == second.version
        assert first_store.current is not None
        assert second_store.current is not None
        assert first_store.current.version == second_store.current.version
    finally:
        if table_ready:
            async with engine.begin() as connection:
                for version in created_versions:
                    await connection.execute(
                        text("DELETE FROM risk_policy_version WHERE version = :version"),
                        {"version": version},
                    )
        await engine.dispose()
