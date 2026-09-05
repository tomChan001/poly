from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
import types
from collections.abc import Awaitable, Callable
from contextlib import suppress
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.container import ApplicationContainer
from backend.app.core.config import Settings, settings
from backend.app.desktop import __main__ as desktop_main
from backend.app.desktop.protocol import RuntimeEvent, RuntimeState, StartCommand
from backend.app.desktop.runtime import (
    ApplicationLifecycle,
    DesktopRuntime,
    LocalMarkerFileSystem,
    upgrade_database,
)
from backend.app.desktop.session import DesktopSession
from backend.app.main import create_app
from backend.app.services.live_runtime import LiveRuntimeService
from backend.app.services.settings import RiskPolicyStore
from backend.app.services.system_control import SystemControl

ROOT = Path(__file__).resolve().parents[4]


def populate_self_test_layout(root: Path) -> dict[str, Path]:
    paths = {
        "frontend": root / "frontend" / "dist" / "index.html",
        "alembic": root / "alembic.ini",
        "migration": root / "migrations" / "env.py",
        "initdb": root / "postgres" / "bin" / "initdb",
        "postgres": root / "postgres" / "bin" / "postgres",
        "pg_isready": root / "postgres" / "bin" / "pg_isready",
        "psql": root / "postgres" / "bin" / "psql",
        "createdb": root / "postgres" / "bin" / "createdb",
        "library": root / "postgres" / "lib" / "libpq.5.dylib",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"packaged")
    for name in ("initdb", "postgres", "pg_isready", "psql", "createdb"):
        executable = paths[name]
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return paths


@pytest.mark.parametrize(
    ("runtime_args", "expected_returncode", "expected_fields"),
    [
        (["--self-test"], 0, {"state": "stopped", "self_test": "ok"}),
        (
            ["--keychain-smoke", "delete", "--account", "invalid"],
            1,
            {"state": "failed", "code": "keychain_smoke_failed"},
        ),
    ],
)
def test_desktop_entrypoint_ignores_hostile_inherited_cwd_dotenv(
    tmp_path: Path,
    runtime_args: list[str],
    expected_returncode: int,
    expected_fields: dict[str, str],
) -> None:
    packaged_root = tmp_path / "packaged-runtime"
    populate_self_test_layout(packaged_root)
    hostile_cwd = tmp_path / "hostile-cwd"
    hostile_cwd.mkdir()
    sentinel = "malicious-dotenv-sentinel"
    (hostile_cwd / ".env").write_text(
        f"RUNTIME_POLL_SECONDS={sentinel}\n", encoding="utf-8"
    )
    probe = """
import runpy
import sys

sys._MEIPASS = sys.argv[1]
runtime_args = sys.argv[2:]
sys.argv = ["poly-runtime", *runtime_args]
runpy.run_module("backend.app.desktop", run_name="__main__")
"""
    environment = os.environ.copy()
    environment.pop("POLY_DESKTOP_MODE", None)
    environment["PYTHONPATH"] = str(ROOT)

    result = subprocess.run(
        [sys.executable, "-c", probe, str(packaged_root), *runtime_args],
        cwd=hostile_cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == expected_returncode, result.stderr
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr
    event = json.loads(result.stdout)
    for key, value in expected_fields.items():
        assert event[key] == value


def test_desktop_mode_import_of_main_skips_unused_default_container(
    tmp_path: Path,
) -> None:
    hostile_cwd = tmp_path / "hostile-cwd"
    hostile_cwd.mkdir()
    (hostile_cwd / ".env").write_text(
        "RUNTIME_POLL_SECONDS=malicious-dotenv-sentinel\n", encoding="utf-8"
    )
    probe = """
import importlib
import os

os.environ["POLY_DESKTOP_MODE"] = "1"
container_module = importlib.import_module("backend.app.container")

def reject_unused_container(cls, *args, **kwargs):
    raise AssertionError("desktop import constructed an unused default container")

container_module.ApplicationContainer.runtime = classmethod(reject_unused_container)
main_module = importlib.import_module("backend.app.main")
assert main_module.app is None
"""
    environment = os.environ.copy()
    environment.pop("POLY_DESKTOP_MODE", None)
    environment["PYTHONPATH"] = str(ROOT)

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=hostile_cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_normal_server_module_still_exports_default_fastapi_app(tmp_path: Path) -> None:
    probe = """
from fastapi import FastAPI
from backend.app.main import app

assert isinstance(app, FastAPI)
"""
    environment = os.environ.copy()
    environment.pop("POLY_DESKTOP_MODE", None)
    environment["PYTHONPATH"] = str(ROOT)

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


class FakePostgres:
    def __init__(self, trace: list[str], database_url: str = "postgresql://local/%db"):
        self.trace = trace
        self.paths = type("Paths", (), {"database_url": database_url})()
        self.exited = asyncio.Event()

    async def start(self) -> None:
        self.trace.append("postgres.start")

    async def stop(self) -> None:
        self.trace.append("postgres.stop")

    async def wait(self) -> None:
        await self.exited.wait()


class FakeContainer:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.system_control = self

    async def disable_opening_async(self, reason: str) -> None:
        self.trace.append(f"opening.disable:{reason}")

    async def close(self) -> None:
        self.trace.append("container.close")


class FakeServer:
    port = 49152

    def __init__(self, trace: list[str], container: FakeContainer) -> None:
        self.trace = trace
        self.container = container
        self.started = False
        self.exited = asyncio.Event()

    async def start(self) -> None:
        self.trace.append("server.start")
        self.started = True

    async def stop(self) -> None:
        self.trace.append("server.stop")
        if self.started:
            self.trace.append("container.close")
            self.started = False

    async def wait(self) -> None:
        await self.exited.wait()


def make_runtime(
    tmp_path: Path,
    trace: list[str],
    *,
    events: list[RuntimeEvent] | None = None,
    postgres: FakePostgres | None = None,
    migrate: Callable[[str, Path], Awaitable[None]] | None = None,
) -> DesktopRuntime:
    fake_postgres = postgres or FakePostgres(trace)
    container = FakeContainer(trace)

    async def default_migrate(database_url: str, project_root: Path) -> None:
        assert database_url == fake_postgres.paths.database_url
        assert project_root == tmp_path
        trace.append("migrate")

    def postgres_factory(_command: StartCommand) -> FakePostgres:
        return fake_postgres

    def application_factory(
        configured_settings: Settings,
        _session: DesktopSession,
    ) -> tuple[ApplicationLifecycle, FastAPI]:
        trace.append("application.create")
        assert configured_settings.opening_enabled is False
        assert configured_settings.database_url == fake_postgres.paths.database_url
        assert (
            configured_settings.credential_service_name
            == "com.poly.desktop.integrations"
        )
        assert configured_settings.local_setup_enabled is True
        return cast(ApplicationLifecycle, container), FastAPI()

    def server_factory(_app: FastAPI) -> FakeServer:
        return FakeServer(trace, container)

    class MarkerFileSystem(LocalMarkerFileSystem):
        def create_atomic(self, path: Path) -> bool:
            created = super().create_atomic(path)
            trace.append("marker.create")
            return created

    return DesktopRuntime(
        project_root=tmp_path,
        postgres_factory=postgres_factory,
        migration_runner=migrate or default_migrate,
        application_factory=application_factory,
        server_factory=server_factory,
        marker_filesystem=MarkerFileSystem(),
        event_sink=(events.append if events is not None else None),
        path_mapper=lambda path: tmp_path / str(path).lstrip("/"),
    )


@pytest.mark.asyncio
async def test_runtime_migrates_before_starting_api_and_worker(tmp_path: Path) -> None:
    trace: list[str] = []
    events: list[RuntimeEvent] = []
    runtime = make_runtime(tmp_path, trace, events=events)

    ready = await runtime.start(
        StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
    )

    assert trace == [
        "marker.create",
        "postgres.start",
        "migrate",
        "application.create",
        "server.start",
    ]
    assert [event.state for event in events] == [
        RuntimeState.PREPARING_DATABASE,
        RuntimeState.MIGRATING,
        RuntimeState.STARTING_SERVICES,
        RuntimeState.READY,
    ]
    assert ready.state is RuntimeState.READY
    assert ready.fields["port"] == 49152
    assert str(ready.fields["bootstrap_path"]).startswith("/desktop/bootstrap/")


@pytest.mark.asyncio
async def test_runtime_settings_do_not_read_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace: list[str] = []
    captured: list[Settings] = []
    runtime = make_runtime(tmp_path, trace)
    original = runtime.application_factory

    def capture(configured: Settings, session: DesktopSession):
        captured.append(configured)
        return original(configured, session)

    runtime.application_factory = capture
    monkeypatch.setenv("OPENING_ENABLED", "true")
    await runtime.start(
        StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
    )

    assert captured[0].opening_enabled is False
    assert captured[0].model_config["env_file"] == ".env"


@pytest.mark.asyncio
async def test_shutdown_closes_opening_before_server_and_database(
    tmp_path: Path,
) -> None:
    trace: list[str] = []
    runtime = make_runtime(tmp_path, trace)
    await runtime.start(
        StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
    )

    stopped = await runtime.stop("application quit")

    assert stopped.state is RuntimeState.STOPPED
    assert trace[-4:] == [
        "opening.disable:application quit",
        "server.stop",
        "container.close",
        "postgres.stop",
    ]
    assert not (tmp_path / "data" / "unclean_shutdown").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_service", ["postgres", "server"])
async def test_unexpected_service_exit_fails_closed_and_keeps_recovery_marker(
    tmp_path: Path,
    failed_service: str,
) -> None:
    trace: list[str] = []
    events: list[RuntimeEvent] = []
    postgres = FakePostgres(trace)
    runtime = make_runtime(tmp_path, trace, events=events, postgres=postgres)
    await runtime.start(
        StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
    )
    server = cast(FakeServer, runtime.server)

    failure_task = asyncio.create_task(runtime.wait_for_failure())
    if failed_service == "postgres":
        postgres.exited.set()
    else:
        server.exited.set()
    failed = await asyncio.wait_for(failure_task, timeout=1)

    assert failed == RuntimeEvent(
        RuntimeState.FAILED,
        {
            "code": "runtime_unavailable",
            "detail": "desktop service stopped unexpectedly",
        },
    )
    assert events[-1] == failed
    assert trace[-3:] == ["server.stop", "container.close", "postgres.stop"]
    assert (tmp_path / "data" / "unclean_shutdown").exists()
    assert await runtime.stop("application quit") == failed


@pytest.mark.asyncio
async def test_service_exit_latches_failure_before_overlapping_parent_shutdown(
    tmp_path: Path,
) -> None:
    trace: list[str] = []
    events: list[RuntimeEvent] = []
    postgres = FakePostgres(trace)
    runtime = make_runtime(tmp_path, trace, events=events, postgres=postgres)
    await runtime.start(
        StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
    )
    server = cast(FakeServer, runtime.server)
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def slow_server_stop() -> None:
        cleanup_entered.set()
        await release_cleanup.wait()
        await FakeServer.stop(server)

    server.stop = slow_server_stop  # type: ignore[method-assign]
    failure_task = asyncio.create_task(runtime.wait_for_failure())
    postgres.exited.set()
    await asyncio.wait_for(cleanup_entered.wait(), timeout=1)

    overlapping_stop = await runtime.stop("parent process ended")

    assert overlapping_stop.state is RuntimeState.FAILED
    assert overlapping_stop.fields["code"] == "runtime_unavailable"
    assert (tmp_path / "data" / "unclean_shutdown").exists()
    release_cleanup.set()
    assert await asyncio.wait_for(failure_task, timeout=1) == overlapping_stop
    assert events[-1] == overlapping_stop


@pytest.mark.asyncio
async def test_unclean_marker_disables_opening_before_worker_lifespan(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "data" / "unclean_shutdown"
    marker.parent.mkdir(parents=True)
    marker.write_text("1", encoding="ascii")
    trace: list[str] = []
    runtime = make_runtime(tmp_path, trace)

    await runtime.start(
        StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
    )

    assert trace.index(
        "opening.disable:unclean desktop shutdown requires reconciliation"
    ) < trace.index("server.start")


@pytest.mark.asyncio
async def test_migration_failure_stops_database_and_leaves_marker(
    tmp_path: Path,
) -> None:
    trace: list[str] = []

    async def fail_migration(_database_url: str, _project_root: Path) -> None:
        trace.append("migrate")
        raise RuntimeError("postgresql://secret-value")

    events: list[RuntimeEvent] = []
    runtime = make_runtime(tmp_path, trace, events=events, migrate=fail_migration)
    failed = await runtime.start(
        StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
    )

    assert failed.state is RuntimeState.FAILED
    assert failed.fields == {
        "code": "migration_failed",
        "detail": "database migration failed",
    }
    assert trace == ["marker.create", "postgres.start", "migrate", "postgres.stop"]
    assert (tmp_path / "data" / "unclean_shutdown").exists()
    assert "secret-value" not in failed.to_json()
    assert events[-1] == failed


@pytest.mark.asyncio
async def test_default_application_factory_closes_container_if_app_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[str] = []
    container = FakeContainer(trace)
    runtime = DesktopRuntime(project_root=tmp_path)
    configured = Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setattr(
        ApplicationContainer,
        "runtime",
        classmethod(lambda _cls, *, configured_settings: container),
    )

    def fail_create_app(*_args: object, **_kwargs: object) -> FastAPI:
        raise ValueError("missing static resources")

    monkeypatch.setattr("backend.app.main.create_app", fail_create_app)

    with pytest.raises(ValueError, match="missing static resources"):
        await runtime._create_application(configured, DesktopSession.create())

    assert trace == ["container.close"]


@pytest.mark.asyncio
async def test_shutdown_failure_still_stops_database_and_keeps_marker(
    tmp_path: Path,
) -> None:
    trace: list[str] = []
    runtime = make_runtime(tmp_path, trace)
    await runtime.start(
        StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
    )
    server = cast(FakeServer, runtime.server)

    async def fail_stop() -> None:
        trace.append("server.stop")
        raise RuntimeError("launch token must not leak")

    server.stop = fail_stop  # type: ignore[method-assign]
    failed = await runtime.stop("application quit")

    assert failed.fields == {
        "code": "shutdown_failed",
        "detail": "runtime shutdown failed",
    }
    assert trace[-3:] == [
        "opening.disable:application quit",
        "server.stop",
        "postgres.stop",
    ]
    assert (tmp_path / "data" / "unclean_shutdown").exists()
    assert "launch token" not in failed.to_json()


@pytest.mark.asyncio
async def test_runtime_start_once_and_stop_is_idempotent(tmp_path: Path) -> None:
    trace: list[str] = []
    runtime = make_runtime(tmp_path, trace)
    before = await runtime.stop("not started")
    await runtime.start(
        StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
    )
    second = await runtime.start(
        StartCommand(PurePosixPath("/other"), PurePosixPath("/run"), "y" * 43)
    )
    await runtime.stop("application quit")
    after = await runtime.stop("already stopped")

    assert before.state is RuntimeState.STOPPED
    assert second.fields["code"] == "runtime_unavailable"
    assert after.state is RuntimeState.STOPPED
    assert trace.count("postgres.start") == 1
    assert trace.count("postgres.stop") == 1


@pytest.mark.asyncio
async def test_cancellation_during_start_cannot_orphan_database(tmp_path: Path) -> None:
    trace: list[str] = []
    migration_entered = asyncio.Event()
    release_migration = asyncio.Event()

    async def migrate(_database_url: str, _project_root: Path) -> None:
        migration_entered.set()
        await release_migration.wait()

    runtime = make_runtime(tmp_path, trace, migrate=migrate)
    task = asyncio.create_task(
        runtime.start(
            StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
        )
    )
    await migration_entered.wait()
    task.cancel()
    release_migration.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert trace[-1] == "postgres.stop"
    assert (tmp_path / "data" / "unclean_shutdown").exists()


@pytest.mark.asyncio
async def test_cancellation_during_stop_finishes_owned_cleanup(tmp_path: Path) -> None:
    trace: list[str] = []
    runtime = make_runtime(tmp_path, trace)
    await runtime.start(
        StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
    )
    server = cast(FakeServer, runtime.server)
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    async def slow_stop() -> None:
        stop_entered.set()
        await release_stop.wait()
        trace.append("server.stop")
        trace.append("container.close")

    server.stop = slow_stop  # type: ignore[method-assign]
    stop_task = asyncio.create_task(runtime.stop("application quit"))
    await stop_entered.wait()
    stop_task.cancel()
    await asyncio.sleep(0)

    assert not stop_task.done()
    release_stop.set()
    with pytest.raises(asyncio.CancelledError):
        await stop_task
    assert trace[-3:] == ["server.stop", "container.close", "postgres.stop"]
    assert not (tmp_path / "data" / "unclean_shutdown").exists()


@pytest.mark.asyncio
async def test_cancellation_while_emitting_shutdown_still_cleans_resources(
    tmp_path: Path,
) -> None:
    trace: list[str] = []
    runtime = make_runtime(tmp_path, trace)
    await runtime.start(
        StartCommand(PurePosixPath("/data"), PurePosixPath("/run"), "x" * 43)
    )
    emitting = asyncio.Event()

    async def blocking_sink(event: RuntimeEvent) -> None:
        if event.state is RuntimeState.SHUTTING_DOWN:
            emitting.set()
            await asyncio.Event().wait()

    runtime.event_sink = blocking_sink
    stop_task = asyncio.create_task(runtime.stop("application quit"))
    await emitting.wait()
    stop_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stop_task
    assert trace[-4:] == [
        "opening.disable:application quit",
        "server.stop",
        "container.close",
        "postgres.stop",
    ]
    assert not (tmp_path / "data" / "unclean_shutdown").exists()


def test_upgrade_database_supplies_explicit_escaped_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    class FakeConfig:
        def __init__(self, filename: str) -> None:
            observed["filename"] = filename
            observed["options"] = {}

        def set_main_option(self, key: str, value: str) -> None:
            cast(dict[str, str], observed["options"])[key] = value

    def upgrade(config: FakeConfig, revision: str) -> None:
        observed["config"] = config
        observed["revision"] = revision

    monkeypatch.setattr("backend.app.desktop.runtime.AlembicConfig", FakeConfig)
    monkeypatch.setattr("backend.app.desktop.runtime.command.upgrade", upgrade)

    upgrade_database("postgresql://local/%2Fsocket", tmp_path)

    assert observed["filename"] == str(tmp_path / "alembic.ini")
    assert observed["options"] == {
        "script_location": str(tmp_path / "migrations"),
        "sqlalchemy.url": "postgresql://local/%%2Fsocket",
    }
    assert observed["revision"] == "head"


def test_migration_environment_does_not_load_settings_for_supplied_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConfig:
        config_file_name = None
        config_ini_section = "alembic"

        def get_main_option(self, name: str) -> str:
            assert name == "sqlalchemy.url"
            return "postgresql+asyncpg://explicit/db"

        def set_main_option(self, _name: str, _value: str) -> None:
            raise AssertionError("supplied migration URL must not be replaced")

    class Transaction:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    fake_context = types.SimpleNamespace(
        config=FakeConfig(),
        is_offline_mode=lambda: True,
        configure=lambda **_kwargs: None,
        begin_transaction=Transaction,
        run_migrations=lambda: None,
    )
    fake_config_module = types.ModuleType("backend.app.core.config")

    def reject_settings(_name: str) -> object:
        if _name == "__path__":
            raise AttributeError(_name)
        raise AssertionError("settings import would load dotenv")

    fake_config_module.__getattr__ = reject_settings  # type: ignore[method-assign]
    monkeypatch.setattr("alembic.context", fake_context)
    monkeypatch.setitem(sys.modules, "backend.app.core.config", fake_config_module)
    migration_env = Path(__file__).resolve().parents[4] / "migrations" / "env.py"
    spec = importlib.util.spec_from_file_location(
        "test_supplied_migration_env", migration_env
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)


def test_migration_environment_replaces_alembic_template_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, str] = {}

    class FakeConfig:
        config_file_name = None
        config_ini_section = "alembic"

        def get_main_option(self, name: str) -> str:
            assert name == "sqlalchemy.url"
            return "driver://user:pass@localhost/dbname"

        def set_main_option(self, name: str, value: str) -> None:
            observed[name] = value

    class Transaction:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    fake_context = types.SimpleNamespace(
        config=FakeConfig(),
        is_offline_mode=lambda: True,
        configure=lambda **_kwargs: None,
        begin_transaction=Transaction,
        run_migrations=lambda: None,
    )
    fake_config_module = types.ModuleType("backend.app.core.config")
    fake_config_module.__dict__["settings"] = types.SimpleNamespace(
        database_url="postgresql+asyncpg://configured/db"
    )
    monkeypatch.setattr("alembic.context", fake_context)
    monkeypatch.setitem(sys.modules, "backend.app.core.config", fake_config_module)
    migration_env = Path(__file__).resolve().parents[4] / "migrations" / "env.py"
    spec = importlib.util.spec_from_file_location(
        "test_default_migration_env", migration_env
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert observed == {"sqlalchemy.url": "postgresql+asyncpg://configured/db"}


def test_container_runtime_uses_supplied_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://explicit/db",
        credential_service_name="explicit-keyring",
        opening_enabled=True,
    )
    observed: dict[str, object] = {}

    def engine(url: str, **_kwargs: object) -> object:
        observed["url"] = url
        return object()

    monkeypatch.setattr("backend.app.container.create_async_engine", engine)
    monkeypatch.setattr(
        "backend.app.container.async_sessionmaker", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(
        "backend.app.container.httpx.AsyncClient", lambda **_k: object()
    )
    monkeypatch.setattr(
        "backend.app.container.KeyringSecretStore",
        lambda name: observed.setdefault("keyring", name) or object(),
    )

    container = ApplicationContainer.runtime(configured_settings=configured)

    assert observed == {
        "url": "postgresql+asyncpg://explicit/db",
        "keyring": "explicit-keyring",
    }
    assert container.system_control.opening_enabled is True


@pytest.mark.asyncio
async def test_create_app_manages_explicit_container_with_explicit_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[str] = []
    container = ApplicationContainer()
    configured = Settings(  # type: ignore[call-arg]
        _env_file=None, runtime_poll_seconds=0.125
    )

    class Control:
        async def load_async(self) -> None:
            trace.append("control.load")

    class Policies:
        async def initialize(self) -> None:
            trace.append("risk.initialize")

    async def close() -> None:
        trace.append("container.close")

    async def loop(_container: object, *, poll_seconds: float) -> None:
        trace.append(f"worker.start:{poll_seconds}")
        try:
            await asyncio.Event().wait()
        finally:
            trace.append("worker.stop")

    container.system_control = cast(SystemControl, Control())
    container.risk_policies = cast(RiskPolicyStore, Policies())
    container.live_runtime = cast(LiveRuntimeService, object())
    monkeypatch.setattr(container, "close", close)
    monkeypatch.setattr("backend.app.main._live_runtime_loop", loop)
    app = create_app(
        container,
        configured_settings=configured,
        manage_runtime_lifespan=True,
    )

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)

    assert trace == [
        "control.load",
        "risk.initialize",
        "worker.start:0.125",
        "worker.stop",
        "container.close",
    ]
    assert app.state.settings is configured


@pytest.mark.asyncio
async def test_security_uses_app_scoped_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", False)
    configured = Settings(  # type: ignore[call-arg]
        _env_file=None, local_setup_enabled=True
    )
    app = create_app(
        ApplicationContainer(),
        allow_local_setup=True,
        configured_settings=configured,
    )
    app.add_api_route(
        "/settings-check",
        lambda request: {"ok": True},
        methods=["GET"],
    )
    transport = ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/api/opportunities")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_stdio_parent_eof_stops_runtime_without_leaking_token() -> None:
    output: list[str] = []
    lines = iter(
        [
            b'{"version":1,"command":"start","data_dir":"/data","runtime_dir":"/run","launch_token":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}\n',
            b"",
        ]
    )

    class Runtime:
        async def start(self, command: StartCommand) -> RuntimeEvent:
            assert len(command.launch_token) == 43
            event = RuntimeEvent(
                RuntimeState.READY,
                {"port": 49152, "bootstrap_path": "/desktop/bootstrap/safe"},
            )
            output.append(event.to_json())
            return event

        async def stop(self, reason: str) -> RuntimeEvent:
            assert reason == "parent process ended"
            event = RuntimeEvent(RuntimeState.STOPPED, {})
            output.append(event.to_json())
            return event

        async def wait_for_failure(self) -> RuntimeEvent:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    result = await desktop_main.run_stdio(
        runtime=cast(DesktopRuntime, Runtime()),
        line_reader=lambda: next(lines),
        event_writer=lambda line: None,
    )

    assert result == 0
    assert all("launch_token" not in line for line in output)


@pytest.mark.asyncio
async def test_stdio_exits_when_an_owned_service_fails_after_ready() -> None:
    start_line = (
        b'{"version":1,"command":"start","data_dir":"/data",'
        b'"runtime_dir":"/run","launch_token":"' + b"x" * 43 + b'"}\n'
    )
    read_count = 0
    stop_reasons: list[str] = []

    async def read_line() -> bytes:
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return start_line
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    class Runtime:
        async def start(self, _command: StartCommand) -> RuntimeEvent:
            return RuntimeEvent(
                RuntimeState.READY,
                {"port": 49152, "bootstrap_path": "/desktop/bootstrap/safe"},
            )

        async def wait_for_failure(self) -> RuntimeEvent:
            await asyncio.sleep(0)
            return RuntimeEvent(
                RuntimeState.FAILED,
                {
                    "code": "runtime_unavailable",
                    "detail": "desktop service stopped unexpectedly",
                },
            )

        async def stop(self, reason: str) -> RuntimeEvent:
            stop_reasons.append(reason)
            return RuntimeEvent(RuntimeState.STOPPED, {})

    result = await asyncio.wait_for(
        desktop_main.run_stdio(
            runtime=cast(DesktopRuntime, Runtime()),
            line_reader=read_line,
            event_writer=lambda _line: None,
        ),
        timeout=1,
    )

    assert result == 1
    assert stop_reasons == []


@pytest.mark.asyncio
async def test_cancelling_stdio_reaps_both_active_wait_tasks() -> None:
    start_line = (
        b'{"version":1,"command":"start","data_dir":"/data",'
        b'"runtime_dir":"/run","launch_token":"' + b"x" * 43 + b'"}\n'
    )
    read_count = 0
    line_waiting = asyncio.Event()
    line_cancelled = asyncio.Event()
    failure_waiting = asyncio.Event()
    failure_cancelled = asyncio.Event()

    async def read_line() -> bytes:
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return start_line
        line_waiting.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            line_cancelled.set()
            raise
        raise AssertionError("unreachable")

    class Runtime:
        async def start(self, _command: StartCommand) -> RuntimeEvent:
            return RuntimeEvent(
                RuntimeState.READY,
                {"port": 49152, "bootstrap_path": "/desktop/bootstrap/safe"},
            )

        async def wait_for_failure(self) -> RuntimeEvent:
            failure_waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                failure_cancelled.set()
                raise
            raise AssertionError("unreachable")

        async def stop(self, _reason: str) -> RuntimeEvent:
            raise AssertionError("cancelled stdio must not claim a clean stop")

    task = asyncio.create_task(
        desktop_main.run_stdio(
            runtime=cast(DesktopRuntime, Runtime()),
            line_reader=read_line,
            event_writer=lambda _line: None,
        )
    )
    await asyncio.wait_for(line_waiting.wait(), timeout=1)
    await asyncio.wait_for(failure_waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert line_cancelled.is_set()
    assert failure_cancelled.is_set()


@pytest.mark.asyncio
async def test_stdio_malformed_command_emits_sanitized_failure() -> None:
    output: list[str] = []
    result = await desktop_main.run_stdio(
        runtime=cast(DesktopRuntime, object()),
        line_reader=lambda: b'{"launch_token":"secret"}\n',
        event_writer=output.append,
    )

    assert result != 0
    assert len(output) == 1
    assert json.loads(output[0]) == {
        "version": 1,
        "state": "failed",
        "code": "invalid_start_command",
        "detail": "invalid desktop command",
    }
    assert "secret" not in output[0]


def test_cancelling_default_stdin_read_does_not_hold_asyncio_run_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    read_started = threading.Event()
    run_finished = threading.Event()
    failures: list[BaseException] = []

    class PipeInput:
        def readline(self) -> bytes:
            read_started.set()
            return os.read(read_fd, 4096)

    monkeypatch.setattr(
        desktop_main.sys,
        "stdin",
        types.SimpleNamespace(buffer=PipeInput()),
    )

    def consume() -> None:
        async def cancel_read() -> None:
            task = asyncio.create_task(desktop_main._read_line(None))
            while not read_started.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        try:
            asyncio.run(cancel_read())
        except BaseException as exc:  # noqa: BLE001 - asserted below
            failures.append(exc)
        finally:
            run_finished.set()

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    try:
        assert read_started.wait(timeout=1)
        assert run_finished.wait(timeout=0.25)
    finally:
        os.close(write_fd)
        consumer.join(timeout=1)
        os.close(read_fd)
    assert failures == []


@pytest.mark.asyncio
async def test_default_stdin_pipe_eof_stops_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "rb", buffering=0)
    monkeypatch.setattr(
        desktop_main.sys,
        "stdin",
        types.SimpleNamespace(buffer=read_stream),
    )
    command = (
        b'{"version":1,"command":"start","data_dir":"/data",'
        b'"runtime_dir":"/run","launch_token":"' + b"x" * 43 + b'"}\n'
    )
    os.write(write_fd, command)
    os.close(write_fd)
    stop_reasons: list[str] = []

    class Runtime:
        async def start(self, _command: StartCommand) -> RuntimeEvent:
            return RuntimeEvent(
                RuntimeState.READY,
                {"port": 49152, "bootstrap_path": "/desktop/bootstrap/safe"},
            )

        async def stop(self, reason: str) -> RuntimeEvent:
            stop_reasons.append(reason)
            return RuntimeEvent(RuntimeState.STOPPED, {})

        async def wait_for_failure(self) -> RuntimeEvent:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    try:
        result = await desktop_main.run_stdio(
            runtime=cast(DesktopRuntime, Runtime()),
            event_writer=lambda _line: None,
        )
    finally:
        read_stream.close()

    assert result == 0
    assert stop_reasons == ["parent process ended"]


def test_self_test_reports_missing_resource_without_starting_runtime(
    tmp_path: Path,
) -> None:
    event = desktop_main.self_test(project_root=tmp_path, temp_root=tmp_path)

    assert event.state is RuntimeState.FAILED
    assert event.fields == {
        "code": "resource_missing",
        "detail": "required packaged resource is unavailable",
    }


def test_self_test_checks_resources_and_writable_temp_directory(tmp_path: Path) -> None:
    populate_self_test_layout(tmp_path)

    event = desktop_main.self_test(project_root=tmp_path, temp_root=tmp_path)

    assert event == RuntimeEvent(RuntimeState.STOPPED, {"self_test": "ok"})


class FakeKeychainSmokeStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest.mark.asyncio
async def test_keychain_smoke_uses_fixed_service_and_never_outputs_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeKeychainSmokeStore()
    services: list[str] = []

    def keyring_store(service: str) -> FakeKeychainSmokeStore:
        services.append(service)
        return store

    monkeypatch.setattr(desktop_main, "KeyringSecretStore", keyring_store)
    account = "ci-smoke-1234-arm64"
    secret = b"synthetic-secret-that-must-not-be-printed"

    written = await desktop_main.keychain_smoke(
        "set", account, secret_stream=BytesIO(secret)
    )
    verified = await desktop_main.keychain_smoke(
        "verify", account, secret_stream=BytesIO(secret)
    )
    deleted = await desktop_main.keychain_smoke("delete", account)

    assert services == ["com.poly.desktop.integrations"] * 3
    assert store.values == {}
    assert [written.state, verified.state, deleted.state] == [
        RuntimeState.STOPPED,
        RuntimeState.STOPPED,
        RuntimeState.STOPPED,
    ]
    output = "".join(event.to_json() for event in (written, verified, deleted))
    assert secret.decode() not in output
    assert account not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account",
    [
        "ci-smoke-",
        "production-account",
        "ci-smoke-../../login",
        "ci-smoke-with_underscore",
        "ci-smoke-" + "x" * 100,
    ],
)
async def test_keychain_smoke_rejects_non_ci_accounts_before_keyring_access(
    monkeypatch: pytest.MonkeyPatch,
    account: str,
) -> None:
    def unexpected_store(_service: str) -> FakeKeychainSmokeStore:
        raise AssertionError("invalid accounts must not reach Keychain")

    monkeypatch.setattr(desktop_main, "KeyringSecretStore", unexpected_store)

    event = await desktop_main.keychain_smoke(
        "set", account, secret_stream=BytesIO(b"synthetic")
    )

    assert event.state is RuntimeState.FAILED
    assert event.fields == {
        "code": "keychain_smoke_failed",
        "detail": "keychain smoke failed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret",
    [b"", b"line-one\nline-two", b"x" * 4097, b"\xff"],
)
async def test_keychain_smoke_rejects_unsafe_stdin_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
    secret: bytes,
) -> None:
    store = FakeKeychainSmokeStore()
    monkeypatch.setattr(desktop_main, "KeyringSecretStore", lambda _service: store)

    event = await desktop_main.keychain_smoke(
        "set", "ci-smoke-safe", secret_stream=BytesIO(secret)
    )

    assert event.state is RuntimeState.FAILED
    assert store.values == {}
    assert "keychain smoke failed" in event.to_json()
    decoded = secret.decode("utf-8", errors="ignore")
    if decoded:
        assert decoded not in event.to_json()


@pytest.mark.asyncio
async def test_keychain_smoke_verify_mismatch_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeKeychainSmokeStore()
    store.values["ci-smoke-safe"] = "stored-value"
    monkeypatch.setattr(desktop_main, "KeyringSecretStore", lambda _service: store)

    event = await desktop_main.keychain_smoke(
        "verify", "ci-smoke-safe", secret_stream=BytesIO(b"different-value")
    )

    assert event.state is RuntimeState.FAILED
    assert event.fields["code"] == "keychain_smoke_failed"
    assert "stored-value" not in event.to_json()
    assert "different-value" not in event.to_json()


def test_keychain_smoke_cli_reads_secret_only_from_stdin_and_emits_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeKeychainSmokeStore()
    secret = b"stdin-only-synthetic-secret"
    monkeypatch.setattr(desktop_main, "KeyringSecretStore", lambda _service: store)
    monkeypatch.setattr(
        desktop_main.sys,
        "stdin",
        types.SimpleNamespace(buffer=BytesIO(secret)),
    )

    result = desktop_main.main(["--keychain-smoke", "set", "--account", "ci-smoke-cli"])

    output = capsys.readouterr().out
    assert result == 0
    assert json.loads(output) == {
        "version": 1,
        "state": "stopped",
        "keychain_smoke": "set",
    }
    assert secret.decode() not in output


@pytest.mark.parametrize(
    "program", ["initdb", "postgres", "pg_isready", "psql", "createdb"]
)
def test_self_test_requires_each_postgres_binary_to_be_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    program: str,
) -> None:
    resources = populate_self_test_layout(tmp_path)
    executable = resources[program]
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert executable.is_file()
    assert executable.resolve().is_relative_to(tmp_path / "postgres" / "bin")
    assert not executable.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def executable_access(path: os.PathLike[str] | str, mode: int) -> bool:
        assert mode == os.X_OK
        return Path(path) != executable

    monkeypatch.setattr(os, "access", executable_access)

    event = desktop_main.self_test(project_root=tmp_path, temp_root=tmp_path)

    assert event.fields == {
        "code": "resource_missing",
        "detail": "required packaged resource is unavailable",
    }


def test_self_test_requires_a_postgres_shared_library(tmp_path: Path) -> None:
    required = [
        tmp_path / "frontend" / "dist" / "index.html",
        tmp_path / "alembic.ini",
        tmp_path / "migrations" / "env.py",
        *(
            tmp_path / "postgres" / "bin" / name
            for name in (
                "initdb",
                "postgres",
                "pg_isready",
                "psql",
                "createdb",
            )
        ),
    ]
    for path in required:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"packaged")

    event = desktop_main.self_test(project_root=tmp_path, temp_root=tmp_path)

    assert event.fields["code"] == "resource_missing"


@pytest.mark.parametrize("resource", ["postgres", "library"])
def test_self_test_rejects_symlinked_resource_escaping_its_subtree(
    tmp_path: Path,
    resource: str,
) -> None:
    resources = populate_self_test_layout(tmp_path)
    target = resources[resource]
    target.unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-{resource}-outside"
    outside.write_bytes(b"external content must not be read")
    try:
        target.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    event = desktop_main.self_test(project_root=tmp_path, temp_root=tmp_path)

    assert event.fields == {
        "code": "resource_missing",
        "detail": "required packaged resource is unavailable",
    }
    assert "external content" not in event.to_json()


def test_self_test_rejects_hard_linked_library_to_outside_file(tmp_path: Path) -> None:
    resources = populate_self_test_layout(tmp_path)
    library = resources["library"]
    library.unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.dylib"
    outside.write_bytes(b"external content must not be read")
    try:
        os.link(outside, library)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    event = desktop_main.self_test(project_root=tmp_path, temp_root=tmp_path)

    assert event.fields == {
        "code": "resource_missing",
        "detail": "required packaged resource is unavailable",
    }
    assert "external content" not in event.to_json()
