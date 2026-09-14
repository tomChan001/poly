"""Acceptance probes for an installed DMG in a disposable CI home only.

The installed app performs every migration. The offline fixture reverses exactly
0010's one-column DDL to represent 0009, without invoking source application code.
No authenticated UI or browser-opening claims are made by these probes.
"""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

HEAD = "0010_risk_policy_liquidity"
REQUIRED_MODULES = {
    "backend.app.core.macos_secrets",
    "backend.app.db.base",
    "backend.app.db.tables",
    "backend.app.adapters.native_fees",
    "backend.app.services.pair_previews",
    "backend.app.services.pair_risk",
}
SEED_SQL = """
BEGIN;
INSERT INTO risk_policy_version (
    version, created_at, minimum_roi, maximum_settlement_days,
    maximum_book_age_seconds, per_trade_limit, per_event_limit, portfolio_limit,
    explicit_cost, risk_buffer, maximum_unhedged_seconds, maximum_unhedged_loss,
    maximum_arrival_gap_seconds
) VALUES
('10000000-0000-4000-8000-000000000001', '2020-01-01T00:00:00Z',
 0.0123, 37, 4.5, 123, 456, 789, 0.003, 0.004, 12, 9, 3),
('10000000-0000-4000-8000-000000000002', '2020-01-02T00:00:00Z',
 0.0234, 41, 5.5, 124, 457, 790, 0.005, 0.006, 13, 10, 4);
INSERT INTO venue_market (
    id, created_at, venue, external_id, title, status, outcomes, rule_url,
    minimum_tick, minimum_quantity, raw_payload
) VALUES (
    '20000000-0000-4000-8000-000000000001', '2020-01-01T00:00:00Z',
    'ci_synthetic', 'feedback-release-sentinel', 'Synthetic preservation sentinel',
    'closed', '{"yes": "synthetic"}', 'https://example.invalid/ci-fixture',
    0.01, 1, '{"fixture": "feedback-release", "never_trade": true}'
);
ALTER TABLE risk_policy_version DROP COLUMN minimum_liquidity_contracts;
UPDATE alembic_version SET version_num = '0009_execution_capital_settlement';
COMMIT;
"""
HISTORY_SQL = """
SELECT jsonb_build_object(
 'risk', (SELECT jsonb_agg(to_jsonb(r) - 'minimum_liquidity_contracts' ORDER BY version)
          FROM risk_policy_version r),
 'sentinel', (SELECT to_jsonb(v) FROM venue_market v
              WHERE id = '20000000-0000-4000-8000-000000000001')
)::text;
"""
OPENING_SQL = "SELECT count(*) FROM system_control WHERE name = 'opening' AND enabled = false"
COLUMN_SQL = """
SELECT count(*) FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'risk_policy_version'
  AND column_name = 'minimum_liquidity_contracts' AND is_nullable = 'NO'
  AND numeric_precision = 38 AND numeric_scale = 18 AND column_default IS NOT NULL
"""


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def tree_hashes(root):
    require(root.is_dir(), "asset directory missing")
    result = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }
    require(result, "asset directory empty")
    return result


def verify_tree(source, installed):
    expected = tree_hashes(source)
    require(expected == tree_hashes(installed), "installed asset mismatch")
    return expected


def verify_modules(modules):
    require(not (REQUIRED_MODULES - set(modules)), "required frozen modules missing")


def validate_smoke_home(home, runner):
    home, runner = home.resolve(), runner.resolve()
    require(
        home.parent.parent == runner and home.name == "home"
        and home.parent.name.startswith("p.") and len(home.parent.name) == 10
        and home.is_dir(),
        "requires an isolated CI smoke home",
    )
    return home


def record_pass(path, phase, commit, details=None):
    evidence = json.loads(path.read_text()) if path.exists() else {
        "commit": commit,
        "passed": [],
        "not_automated": [
            "authenticated WebView feedback forms and native external-browser opening",
            "real venue credentials, API calls and order submission",
        ],
    }
    require(evidence["commit"] == commit, "acceptance evidence commit mismatch")
    if phase not in evidence["passed"]:
        evidence["passed"].append(phase)
    if details is not None:
        evidence[phase] = details
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


def command(argv, **kwargs):
    return subprocess.run(
        [str(arg) for arg in argv], check=True, capture_output=True,
        text=True, timeout=30, **kwargs,
    ).stdout.strip()


def verify_bundle(runtime, source):
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(runtime / "poly-runtime"))
    pyz_names = [name for name, entry in archive.toc.items() if entry[-1] == "z"]
    require(len(pyz_names) == 1, "expected one embedded Python archive")
    verify_modules(archive.open_embedded_archive(pyz_names[0]).toc)
    resources = runtime / "_internal"
    frontend = verify_tree(source / "frontend/dist", resources / "frontend/dist")
    migration = Path("migrations/versions") / f"{HEAD}.py"
    expected = hashlib.sha256((source / migration).read_bytes()).hexdigest()
    require(
        hashlib.sha256((resources / migration).read_bytes()).hexdigest() == expected,
        "installed migration asset mismatch",
    )
    verify_self_test(command([runtime / "poly-runtime", "--self-test"]))
    return {"frontend_sha256": frontend, "migration_sha256": expected,
            "frozen_modules": sorted(REQUIRED_MODULES), "frozen_self_test": "passed"}


def verify_self_test(output):
    require(json.loads(output) == {"version": 1, "state": "stopped", "self_test": "ok"},
            "frozen self-test failed")


class SmokeDatabase:
    def __init__(self, runtime, home):
        self.home = home
        self.data = home / "Library/Application Support/Poly/postgres"
        require(self.data.resolve().is_relative_to(home), "database escaped smoke home")
        require((self.data / "PG_VERSION").is_file(), "installed app did not create database")
        self.bin = runtime / "_internal/postgres/bin"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
        self.env["PGSSLMODE"] = "disable"
        self.env["PGCONNECT_TIMEOUT"] = "3"
        self.socket = None

    def use_live_socket(self):
        lines = (self.data / "postmaster.pid").read_text().splitlines()
        require(len(lines) >= 5 and lines[3] == "5432", "unexpected packaged PostgreSQL state")
        self.set_socket(lines[4])

    def set_socket(self, value):
        socket = Path(value).resolve()
        require(socket.is_relative_to(self.home) and socket.is_dir(), "socket escaped smoke home")
        require(socket.stat().st_mode & 0o077 == 0, "PostgreSQL socket directory is not private")
        self.socket = socket

    def sql(self, sql):
        require(self.socket is not None, "private socket not resolved")
        return command([
            self.bin / "psql", "-X", "-w", "-h", self.socket, "-p", "5432",
            "-U", "poly", "-d", "poly", "-A", "-t", "-v", "ON_ERROR_STOP=1",
            "-c", sql,
        ], env=self.env)

    def verify_head(self, *, require_persisted_opening=True):
        require(self.sql("SELECT version_num FROM alembic_version") == HEAD, "migration head is not 0010")
        require(self.sql(COLUMN_SQL) == "1", "liquidity schema missing or invalid")
        if require_persisted_opening:
            require(self.sql(OPENING_SQL) == "1", "persisted opening control is not disabled")

    def history(self):
        return hashlib.sha256(self.sql(HISTORY_SQL).encode()).hexdigest()

    def seed_previous(self):
        require(not (self.data / "postmaster.pid").exists(), "app PostgreSQL must be stopped before seeding")
        options = shlex.split((self.data / "postmaster.opts").read_text())
        sockets = [value.split("=", 1)[1] for value in options if value.startswith("unix_socket_directories=")]
        require(len(sockets) == 1, "cannot resolve previous app private socket")
        self.set_socket(sockets[0])
        with (self.home.parent / "acceptance-postgres.log").open("a") as log:
            process = subprocess.Popen([
                str(self.bin / "postgres"), "-D", str(self.data),
                "-c", "listen_addresses=", "-c", f"unix_socket_directories={self.socket}",
                "-c", "unix_socket_permissions=0700", "-c", "port=5432",
                "-c", "logging_collector=off",
            ], env=self.env, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    require(process.poll() is None, "fixture PostgreSQL exited before readiness")
                    ready = subprocess.run([
                        str(self.bin / "pg_isready"), "-h", str(self.socket), "-p", "5432",
                        "-U", "poly", "-d", "poly", "-q",
                    ], env=self.env, timeout=5, check=False)
                    if ready.returncode == 0:
                        break
                    time.sleep(0.1)
                else:
                    raise ValueError("fixture PostgreSQL readiness timed out")
                self.verify_head()
                self.sql(SEED_SQL)
                require(self.sql("SELECT version_num FROM alembic_version") ==
                        "0009_execution_capital_settlement", "prior revision was not seeded")
                require(self.sql(COLUMN_SQL) == "0", "prior schema still has liquidity column")
                return self.history()
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    raise ValueError("fixture PostgreSQL did not stop cleanly") from None
                require(process.returncode == 0, "fixture PostgreSQL shutdown failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["bundle", "fresh", "seed-0009", "upgraded", "persisted"])
    for argument in ("runtime", "home", "source", "evidence"):
        parser.add_argument(f"--{argument}", type=Path, required=True)
    args = parser.parse_args()
    require(os.environ.get("CI") == "true", "acceptance only runs in CI")
    home = validate_smoke_home(args.home, Path(os.environ["RUNNER_TEMP"]))
    runtime = args.runtime.resolve()
    require(runtime == home.parent / "Applications/Poly.app/Contents/Resources/poly-runtime",
            "runtime is not the installed CI smoke app")
    details = None
    baseline = home.parent / "feedback-history.sha256"
    if args.phase == "bundle":
        details = verify_bundle(runtime, args.source.resolve())
    else:
        database = SmokeDatabase(runtime, home)
        if args.phase == "seed-0009":
            baseline.write_text(database.seed_previous())
        else:
            database.use_live_socket()
            database.verify_head(require_persisted_opening=args.phase != "fresh")
            if args.phase != "fresh":
                require(database.history() == baseline.read_text(), "historical data changed during upgrade or restart")
                require(database.sql("SELECT count(*) FROM risk_policy_version "
                        "WHERE version IN ('10000000-0000-4000-8000-000000000001', "
                        "'10000000-0000-4000-8000-000000000002') "
                        "AND minimum_liquidity_contracts = 1") == "2",
                        "historical policy liquidity default was not preserved")
    record_pass(args.evidence, args.phase, os.environ["GITHUB_SHA"], details)
    print(f"feedback acceptance passed: {args.phase}")


if __name__ == "__main__":
    main()
