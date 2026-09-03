import asyncio
import os
import re
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

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

        runner_store = PostgresRiskPolicyStore(sessions)
        assert await runner_store.refresh() == second
        assert runner_store.current == second
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
    schema = f"risk_policy_test_{uuid4().hex}"
    assert re.fullmatch(r"risk_policy_test_[0-9a-f]{32}", schema)
    schema_identifier = f'"{schema}"'
    admin_engine = create_async_engine(database_url)
    engine = create_async_engine(
        database_url,
        connect_args={"server_settings": {"search_path": schema}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    schema_created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema_identifier}"))
            created_schema = await connection.scalar(
                text(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name = :schema"
                ),
                {"schema": schema},
            )
            assert created_schema == schema
        schema_created = True

        async with engine.begin() as connection:
            current_schema = await connection.scalar(text("SELECT current_schema()"))
            assert current_schema == schema
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
            await connection.execute(
                text(
                    """
                    CREATE FUNCTION pause_risk_policy_seed()
                    RETURNS trigger AS $$
                    BEGIN
                        PERFORM pg_sleep(0.25);
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TRIGGER pause_risk_policy_seed_insert
                    BEFORE INSERT ON risk_policy_version
                    FOR EACH ROW EXECUTE FUNCTION pause_risk_policy_seed()
                    """
                )
            )
            count = await connection.scalar(text("SELECT count(*) FROM risk_policy_version"))
            assert count == 0

        first_store = PostgresRiskPolicyStore(sessions)
        second_store = PostgresRiskPolicyStore(sessions)
        first_task = asyncio.create_task(first_store.initialize())
        await _wait_for_risk_policy_lock(admin_engine, granted=True)
        second_task = asyncio.create_task(second_store.initialize())
        await _wait_for_risk_policy_lock(admin_engine, granted=False)
        first, second = await asyncio.gather(first_task, second_task)

        assert first.version == second.version
        assert first_store.current is not None
        assert second_store.current is not None
        assert first_store.current.version == second_store.current.version
        async with engine.connect() as connection:
            count = await connection.scalar(text("SELECT count(*) FROM risk_policy_version"))
        assert count == 1
    finally:
        await engine.dispose()
        if schema_created:
            assert re.fullmatch(r"risk_policy_test_[0-9a-f]{32}", schema)
            async with admin_engine.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA {schema_identifier} CASCADE"))
        await admin_engine.dispose()


async def _wait_for_risk_policy_lock(engine, *, granted: bool) -> None:
    for _ in range(50):
        async with engine.connect() as connection:
            count = await connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM pg_locks
                    WHERE locktype = 'advisory'
                      AND objid = hashtext('poly-risk-policy-version')
                      AND granted = :granted
                    """
                ),
                {"granted": granted},
            )
        if count:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"risk policy advisory lock did not reach granted={granted}")
