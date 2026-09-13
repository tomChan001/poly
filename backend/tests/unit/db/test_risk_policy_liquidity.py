from dataclasses import replace
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import RowMapping

from backend.app.db.risk_policy import PostgresRiskPolicyStore, _insert, _snapshot
from backend.app.db.tables import RiskPolicyVersion
from backend.app.services.settings import RiskPolicyInput


@pytest.mark.asyncio
async def test_liquidity_is_saved_and_restored_in_durable_snapshot() -> None:
    value = replace(RiskPolicyInput.defaults(), minimum_liquidity_contracts=Decimal("25.125"))
    policy = _snapshot(value)
    session = AsyncMock()

    await _insert(session, policy)
    statement, parameters = session.execute.call_args.args
    assert "minimum_liquidity_contracts" in str(statement)
    assert parameters["minimum_liquidity_contracts"] == Decimal("25.125")
    restored = PostgresRiskPolicyStore._policy(cast(RowMapping, parameters))
    assert restored == policy
    value.minimum_liquidity_contracts = Decimal(90)
    assert restored.minimum_liquidity_contracts == Decimal("25.125")


def test_risk_policy_metadata_has_safe_liquidity_default() -> None:
    column = RiskPolicyVersion.__table__.columns["minimum_liquidity_contracts"]
    assert not column.nullable
    assert column.server_default is not None
    assert str(column.server_default.arg) == "1"


def test_liquidity_migration_backfills_existing_policies_and_tolerates_current_metadata(
    monkeypatch,
) -> None:
    path = Path("migrations/versions/0010_risk_policy_liquidity.py")
    assert path.exists(), "liquidity migration must exist"
    spec = spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert migration.down_revision == "0009_execution_capital_settlement"
    assert statements == [
        (
            "ALTER TABLE risk_policy_version "
            "ADD COLUMN IF NOT EXISTS minimum_liquidity_contracts NUMERIC(38, 18) "
            "NOT NULL DEFAULT 1"
        )
    ]
    statements.clear()
    migration.downgrade()
    assert statements == [
        "ALTER TABLE risk_policy_version DROP COLUMN IF EXISTS minimum_liquidity_contracts"
    ]
