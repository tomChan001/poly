import importlib.util
from pathlib import Path


def _migration_module():
    path = (
        Path(__file__).resolve().parents[4]
        / "migrations"
        / "versions"
        / "0009_execution_capital_settlement.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0009", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_execution_capital_settlement_migration_backfills_and_indexes_candidates(
    monkeypatch,
) -> None:
    migration = _migration_module()
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    sql = "\n".join(statements)
    assert migration.down_revision == "0008_incident_remediation"
    assert statements[0] == (
        "ALTER TABLE alembic_version "
        "ALTER COLUMN version_num TYPE VARCHAR(64)"
    )
    assert "ADD COLUMN IF NOT EXISTS capital_settled BOOLEAN" in sql
    assert "WHEN record.state = 'submitted' THEN FALSE" in sql
    assert "FROM capital_reservation AS reservation" in sql
    assert "reservation.status = 'active'" in sql
    assert "WHERE record.capital_settled IS NULL" in sql
    assert "ALTER COLUMN capital_settled SET NOT NULL" in sql
    assert "ix_execution_record_recovery_candidates" in sql
    assert "WHERE state = 'submitted' OR capital_settled = FALSE" in sql
