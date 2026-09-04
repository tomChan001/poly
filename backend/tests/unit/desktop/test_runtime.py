from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from collections.abc import Awaitable, Callable
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


class FakePostgres:
    def __init__(self, trace: list[str], database_url: str = "postgresql://local/%db"):
        self.trace = trace
        self.paths = type("Paths", (), {"database_url": database_url})()

    async def start(self) -> None:
        self.trace.append("postgres.start")

    async def stop(self) -> None:
        self.trace.append("postgres.stop")


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

    async def start(self) -> None:
        self.trace.append("server.start")
        self.started = True

    async def stop(self) -> None:
        self.trace.append("server.stop")
        if self.started:
            self.trace.append("container.close")
            self.started = False


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

    result = await desktop_main.run_stdio(
        runtime=cast(DesktopRuntime, Runtime()),
        line_reader=lambda: next(lines),
        event_writer=lambda line: None,
    )

    assert result == 0
    assert all("launch_token" not in line for line in output)


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
    shared_library = tmp_path / "postgres" / "lib" / "libpq.5.dylib"
    shared_library.parent.mkdir(parents=True)
    shared_library.write_bytes(b"packaged")

    event = desktop_main.self_test(project_root=tmp_path, temp_root=tmp_path)

    assert event == RuntimeEvent(RuntimeState.STOPPED, {"self_test": "ok"})


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
