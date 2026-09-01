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


def test_initial_migration_is_guarded_against_model_drift() -> None:
    migration = load_initial_migration()

    assert migration.EXPECTED_TABLES == set(Base.metadata.tables)


def test_initial_migration_makes_audit_events_append_only() -> None:
    migration = load_initial_migration()

    assert "BEFORE UPDATE OR DELETE ON audit_event" in migration.AUDIT_TRIGGER_SQL
    assert "audit_event is append-only" in migration.AUDIT_FUNCTION_SQL
