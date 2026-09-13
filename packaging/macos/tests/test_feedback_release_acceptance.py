"""Portable contracts for acceptance of the actual installed native artifact."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "packaging/macos/accept-feedback-release.py"


def load_helper():
    assert HELPER.is_file(), "installed feedback artifact acceptance is missing"
    spec = importlib.util.spec_from_file_location("feedback_acceptance", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installed_acceptance_gates_every_database_phase_and_relaunch():
    workflow = (ROOT / ".github/workflows/macos-desktop.yml").read_text()
    smoke = workflow.split("- name: Install DMG into an isolated home and smoke test", 1)[1]
    smoke = smoke.split("- name: Remove temporary signing keychain", 1)[0]
    for phase in ("bundle", "fresh", "seed-0009", "upgraded", "persisted"):
        assert f"accept_feedback {phase}" in smoke, f"missing installed {phase} acceptance"
    assert smoke.count("wait_for_health\n") >= 3
    assert 'mktemp -d "${RUNNER_TEMP}/p.XXXXXXXX"' in smoke
    assert smoke.count('"${APP_EXECUTABLE}" >>"${APP_STDOUT}" 2>>"${APP_STDERR}" &') >= 4
    assert 'app_pid=$!' in smoke and 'second_pid=$!' in smoke
    assert "%{http_code}" in smoke and "403" in smoke
    assert "feedback-release-evidence.json" in smoke
    assert 'ACCEPTANCE_PYTHON="${GITHUB_WORKSPACE}/.venv/bin/python"' in smoke
    assert workflow.index("test_feedback_release_acceptance.py") < workflow.index("npm run build")


def test_frontend_manifest_rejects_changed_or_extra_installed_files(tmp_path):
    helper = load_helper()
    source, installed = tmp_path / "source", tmp_path / "installed"
    for directory in (source, installed):
        directory.mkdir()
        (directory / "index.html").write_text("same commit")
    assert helper.verify_tree(source, installed) == helper.tree_hashes(source)
    (installed / "index.html").write_text("stale")
    with pytest.raises(ValueError, match="asset mismatch"):
        helper.verify_tree(source, installed)
    (installed / "index.html").write_text("same commit")
    (installed / "extra.js").write_text("stale")
    with pytest.raises(ValueError, match="asset mismatch"):
        helper.verify_tree(source, installed)


def test_frozen_module_inventory_rejects_any_missing_feedback_module():
    helper = load_helper()
    required = set(helper.REQUIRED_MODULES)
    assert len(required) == 5
    helper.verify_modules(required)
    for module in required:
        with pytest.raises(ValueError, match="frozen modules missing"):
            helper.verify_modules(required - {module})


def test_database_scope_rejects_real_home_and_non_smoke_directory(tmp_path):
    helper = load_helper()
    runner = tmp_path / "runner"
    home = runner / "p.12345678" / "home"
    home.mkdir(parents=True)
    assert helper.validate_smoke_home(home, runner) == home.resolve()
    with pytest.raises(ValueError, match="isolated CI smoke home"):
        helper.validate_smoke_home(tmp_path, runner)
    with pytest.raises(ValueError, match="isolated CI smoke home"):
        helper.validate_smoke_home(runner / "real-user" / "home", runner)


def test_evidence_only_records_completed_phases(tmp_path):
    helper = load_helper()
    evidence = tmp_path / "evidence.json"
    helper.record_pass(evidence, "fresh", "abc123")
    import json

    result = json.loads(evidence.read_text())
    assert result["passed"] == ["fresh"]
    assert "authenticated WebView" in result["not_automated"][0]
    helper.record_pass(evidence, "upgraded", "abc123")
    assert json.loads(evidence.read_text())["passed"] == ["fresh", "upgraded"]


def test_prior_schema_fixture_is_bounded_and_preserves_historical_values():
    helper = load_helper()
    sql = helper.SEED_SQL
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "DROP COLUMN minimum_liquidity_contracts" in sql
    assert "0009_execution_capital_settlement" in sql
    assert "INSERT INTO risk_policy_version" in sql
    assert "INSERT INTO venue_market" in sql
    assert "DELETE FROM" not in sql and "DROP TABLE" not in sql
    assert "minimum_liquidity_contracts" in helper.HISTORY_SQL
    assert "system_control" in helper.OPENING_SQL


def test_fresh_database_allows_uninitialized_opening_but_relaunch_requires_durable_disable():
    helper = load_helper()
    database = object.__new__(helper.SmokeDatabase)
    database.sql = lambda sql: (
        helper.HEAD if "alembic_version" in sql else "1" if sql == helper.COLUMN_SQL else "0"
    )
    database.verify_head(require_persisted_opening=False)
    with pytest.raises(ValueError, match="opening control"):
        database.verify_head()


def test_installed_self_test_accepts_real_versioned_protocol_and_rejects_failure():
    from backend.app.desktop.protocol import RuntimeEvent, RuntimeState

    helper = load_helper()
    assert hasattr(helper, "verify_self_test"), "missing installed versioned protocol validation"
    helper.verify_self_test(RuntimeEvent(RuntimeState.STOPPED, {"self_test": "ok"}).to_json())
    with pytest.raises(ValueError, match="frozen self-test"):
        helper.verify_self_test(RuntimeEvent(RuntimeState.FAILED, {"code": "resource_missing"}).to_json())
