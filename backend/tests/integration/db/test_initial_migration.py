from collections.abc import Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from backend.app.db import tables  # noqa: F401
from backend.app.db.base import Base


def load_initial_migration():
    path = Path("migrations/versions/0001_initial_schema.py")
    spec = spec_from_file_location("initial_schema", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_migration(filename: str):
    path = Path("migrations/versions") / filename
    spec = spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record_kwargs(calls: list[dict[str, object]]) -> Callable[..., None]:
    def record(*_args: object, **kwargs: object) -> None:
        calls.append(kwargs)

    return record


def test_initial_migration_is_guarded_against_model_drift() -> None:
    migration = load_initial_migration()

    assert migration.EXPECTED_TABLES == set(Base.metadata.tables)


def test_initial_migration_makes_audit_events_append_only() -> None:
    migration = load_initial_migration()

    assert "BEFORE UPDATE OR DELETE ON audit_event" in migration.AUDIT_TRIGGER_SQL
    assert "audit_event is append-only" in migration.AUDIT_FUNCTION_SQL


def test_projection_migrations_tolerate_preexisting_test_tables(monkeypatch) -> None:
    for filename in (
        "0003_runtime_execution_history.py",
        "0004_executable_pairs.py",
    ):
        migration = load_migration(filename)
        calls: list[dict[str, object]] = []
        monkeypatch.setattr(
            migration.op,
            "create_table",
            record_kwargs(calls),
        )
        monkeypatch.setattr(
            migration.op,
            "create_index",
            record_kwargs(calls),
        )

        migration.upgrade()

        assert calls == [
            {"if_not_exists": True},
            {"if_not_exists": True},
        ]


def test_risk_policy_migration_tolerates_table_created_by_current_metadata(monkeypatch) -> None:
    migration = load_migration("0007_risk_policy_versions.py")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(migration.op, "create_table", record_kwargs(calls))

    migration.upgrade()

    assert calls == [{"if_not_exists": True}]


def test_incident_remediation_migration_backfills_existing_incidents(monkeypatch) -> None:
    migration = load_migration("0008_incident_remediation.py")
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert statements == [
        (
            "ALTER TABLE execution_incident "
            "ADD COLUMN IF NOT EXISTS remediation_status VARCHAR(32) "
            "NOT NULL DEFAULT 'pending'"
        ),
        (
            "ALTER TABLE execution_incident "
            "ADD COLUMN IF NOT EXISTS remediation_client_order_id VARCHAR(255)"
        ),
        (
            "ALTER TABLE execution_incident "
            "ADD COLUMN IF NOT EXISTS remediation_venue VARCHAR(32)"
        ),
    ]
