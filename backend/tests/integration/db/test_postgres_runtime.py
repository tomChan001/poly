import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.db.base import Base
from backend.app.db.capital import PostgresCapitalLedger
from backend.app.db.integration_config import PostgresIntegrationConfigRepository
from backend.app.db.operational_control import PostgresOperationalControlStore
from backend.app.services.integration_config import (
    IntegrationConfigRecord,
    IntegrationEnvironment,
    IntegrationProvider,
)
from backend.app.services.system_control import SystemControl

ADMIN_URL_ENV = "TEST_POSTGRES_ADMIN_URL"


def database_url(admin_url: str, database: str, *, for_sqlalchemy: bool) -> str:
    """Build native asyncpg and SQLAlchemy URLs for an isolated test database."""
    parsed = urlsplit(admin_url)
    scheme = "postgresql+asyncpg" if for_sqlalchemy else "postgresql"
    return urlunsplit((scheme, parsed.netloc, f"/{database}", "", ""))


@pytest.mark.asyncio
async def test_initial_migration_runs_on_postgres_and_protects_audit_events() -> None:
    admin_url = os.getenv(ADMIN_URL_ENV)
    if not admin_url:
        pytest.skip(f"{ADMIN_URL_ENV} is required for the destructive database test")

    database = f"poly_migration_test_{uuid4().hex}"
    admin = await asyncpg.connect(admin_url)
    await admin.execute(f'CREATE DATABASE "{database}"')
    await admin.close()

    native_url = database_url(admin_url, database, for_sqlalchemy=False)
    migration_url = database_url(admin_url, database, for_sqlalchemy=True)
    try:
        environment = os.environ.copy()
        environment["DATABASE_URL"] = migration_url
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=Path.cwd(),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        connection = await asyncpg.connect(native_url)
        try:
            table_names = set(
                await connection.fetchval(
                    "SELECT array_agg(tablename) FROM pg_tables WHERE schemaname = 'public'"
                )
            )
            assert set(Base.metadata.tables) <= table_names
            assert "integration_config" in table_names

            integration_columns = set(
                await connection.fetchval(
                    """
                    SELECT array_agg(column_name)
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'integration_config'
                    """
                )
            )
            assert integration_columns == {
                "provider",
                "enabled",
                "environment",
                "base_url",
                "configuration",
                "version",
                "updated_at",
                "updated_by",
            }

            engine = create_async_engine(migration_url)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            repository = PostgresIntegrationConfigRepository(
                sessions
            )
            try:
                first = await repository.upsert(
                    IntegrationConfigRecord(
                        provider=IntegrationProvider.ODDPOOL,
                        enabled=False,
                        environment=IntegrationEnvironment.SANDBOX,
                        base_url="https://oddpool.test",
                        updated_by="runtime-test",
                    )
                )
                second = await repository.upsert(
                    IntegrationConfigRecord(
                        provider=IntegrationProvider.ODDPOOL,
                        enabled=False,
                        environment=IntegrationEnvironment.PRODUCTION,
                        base_url="https://oddpool.example",
                        updated_by="runtime-test",
                    )
                )
                persisted = await repository.get(IntegrationProvider.ODDPOOL)

                ledger = PostgresCapitalLedger(sessions)
                ledger.sync_available_balances(
                    kalshi_available=Decimal(100),
                    polymarket_available=Decimal(100),
                )
                reservation = await ledger.reserve_pair(
                    "durable-correlation",
                    Decimal(10),
                    Decimal(8),
                    event_id="event-1",
                )
                restarted_ledger = PostgresCapitalLedger(sessions)
                await restarted_ledger.refresh()
                restored = restarted_ledger.get_pair("durable-correlation")
                assert restored is not None
                assert restored.evidence_id == reservation.evidence_id
                assert restarted_ledger.event_exposure("event-1") == Decimal(18)
                await restarted_ledger.convert_pair("durable-correlation")
                after_conversion = PostgresCapitalLedger(sessions)
                await after_conversion.refresh()
                assert after_conversion.portfolio_exposure() == Decimal(18)

                control_store = PostgresOperationalControlStore(sessions)
                control = SystemControl(
                    opening_enabled=True,
                    reason="configured default",
                    store=control_store,
                )
                saved = await control.disable_opening_async(
                    "incident",
                    changed_by="runtime-test",
                )
                restarted_control = SystemControl(
                    opening_enabled=True,
                    reason="configured default",
                    store=control_store,
                )
                restored_control = await restarted_control.load_async()

                await ledger.reserve_pair(
                    "released-correlation",
                    Decimal(3),
                    Decimal(2),
                    event_id="event-2",
                )
                await ledger.release_pair("released-correlation")
                with pytest.raises(ValueError, match="reservation is released"):
                    await ledger.reserve_pair(
                        "released-correlation",
                        Decimal(3),
                        Decimal(2),
                        event_id="event-2",
                    )
            finally:
                await engine.dispose()

            assert first.version == 1
            assert second.version == 2
            assert persisted == second
            assert saved.version == 1
            assert restored_control.opening_enabled is False
            assert restored_control.reason == "incident"
            assert restored_control.version == 1
            assert restarted_control.version == 1

            event_id = uuid4()
            await connection.execute(
                """
                INSERT INTO audit_event (
                    id, created_at, correlation_id, actor, event_type,
                    object_type, object_id, before_state, after_state, evidence_hash
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10)
                """,
                event_id,
                datetime.now(UTC),
                "runtime-test",
                "pytest",
                "migration_verified",
                "database",
                database,
                json.dumps({"state": "before"}),
                json.dumps({"state": "after"}),
                "0" * 64,
            )

            # The append-only guarantee must be enforced by PostgreSQL itself,
            # including writes that bypass the application service layer.
            with pytest.raises(asyncpg.PostgresError, match="audit_event is append-only"):
                await connection.execute(
                    "UPDATE audit_event SET actor = 'tampered' WHERE id = $1", event_id
                )
        finally:
            await connection.close()
    finally:
        admin = await asyncpg.connect(admin_url)
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()
