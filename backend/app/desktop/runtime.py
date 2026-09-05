from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePath
from typing import Protocol

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from backend.app.container import ApplicationContainer
from backend.app.core.config import Settings
from backend.app.desktop.postgres import PostgresManager, PostgresPaths
from backend.app.desktop.protocol import RuntimeEvent, RuntimeState, StartCommand
from backend.app.desktop.server import LoopbackServer
from backend.app.desktop.session import DesktopSession

UNCLEAN_SHUTDOWN_REASON = "unclean desktop shutdown requires reconciliation"


class DatabasePaths(Protocol):
    @property
    def database_url(self) -> str: ...


class PostgresLifecycle(Protocol):
    @property
    def paths(self) -> DatabasePaths: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def wait(self) -> None: ...


class ApplicationLifecycle(Protocol):
    @property
    def system_control(self) -> OpeningControlLifecycle: ...

    async def close(self) -> None: ...


class OpeningControlLifecycle(Protocol):
    async def disable_opening_async(self, reason: str) -> object: ...


class ServerLifecycle(Protocol):
    @property
    def port(self) -> int: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def wait(self) -> None: ...


class MarkerFileSystem(Protocol):
    def create_atomic(self, path: Path) -> bool:
        """Create the marker and return whether it already existed."""

    def remove(self, path: Path) -> None: ...


EventSink = Callable[[RuntimeEvent], None | Awaitable[None]]
PostgresFactory = Callable[[StartCommand], PostgresLifecycle]
MigrationRunner = Callable[[str, Path], None | Awaitable[None]]
ApplicationResult = tuple[ApplicationLifecycle, FastAPI]
ApplicationFactory = Callable[
    [Settings, DesktopSession], ApplicationResult | Awaitable[ApplicationResult]
]
ServerFactory = Callable[[FastAPI], ServerLifecycle]
PathMapper = Callable[[PurePath], Path]


class LocalMarkerFileSystem:
    def create_atomic(self, path: Path) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="ascii") as marker:
                marker.write("1\n")
        except FileExistsError:
            return True
        return False

    def remove(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def packaged_project_root() -> Path:
    bundled_root = getattr(sys, "_MEIPASS", None)
    if isinstance(bundled_root, str):
        return Path(bundled_root)
    return Path(__file__).resolve().parents[3]


def upgrade_database(database_url: str, project_root: Path) -> None:
    config = AlembicConfig(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


async def upgrade_database_async(database_url: str, project_root: Path) -> None:
    await asyncio.to_thread(upgrade_database, database_url, project_root)


def _default_path_mapper(path: PurePath) -> Path:
    return Path(str(path))


class DesktopRuntime:
    def __init__(
        self,
        *,
        project_root: Path | None = None,
        postgres_factory: PostgresFactory | None = None,
        migration_runner: MigrationRunner | None = None,
        application_factory: ApplicationFactory | None = None,
        server_factory: ServerFactory | None = None,
        marker_filesystem: MarkerFileSystem | None = None,
        event_sink: EventSink | None = None,
        path_mapper: PathMapper | None = None,
    ) -> None:
        self.project_root = (project_root or packaged_project_root()).resolve()
        self._path_mapper = path_mapper or _default_path_mapper
        self.postgres_factory: PostgresFactory = (
            postgres_factory if postgres_factory is not None else self._create_postgres
        )
        self.migration_runner = migration_runner or upgrade_database_async
        self.application_factory = application_factory or self._create_application
        self.server_factory = server_factory or LoopbackServer
        self.marker_filesystem = marker_filesystem or LocalMarkerFileSystem()
        self.event_sink = event_sink

        self.postgres: PostgresLifecycle | None = None
        self.container: ApplicationLifecycle | None = None
        self.server: ServerLifecycle | None = None
        self._marker_path: Path | None = None
        self._start_called = False
        self._ready = False
        self._cleanup_task: asyncio.Task[bool] | None = None
        self._terminal_event: RuntimeEvent | None = None

    async def start(self, start_command: StartCommand) -> RuntimeEvent:
        if self._start_called:
            return await self._failure(
                "runtime_unavailable",
                "desktop runtime is already started",
                terminal=False,
            )
        self._start_called = True
        data_dir = self._path_mapper(start_command.data_dir)
        self._marker_path = data_dir / "unclean_shutdown"
        phase = RuntimeState.PREPARING_DATABASE
        try:
            marker_existed = self.marker_filesystem.create_atomic(self._marker_path)
            await self._emit(RuntimeEvent(phase, {}))
            postgres = self.postgres_factory(start_command)
            self.postgres = postgres
            await postgres.start()

            phase = RuntimeState.MIGRATING
            await self._emit(RuntimeEvent(phase, {}))
            migration = self.migration_runner(
                postgres.paths.database_url, self.project_root
            )
            if inspect.isawaitable(migration):
                await migration

            phase = RuntimeState.STARTING_SERVICES
            await self._emit(RuntimeEvent(phase, {}))
            configured_settings = Settings(  # type: ignore[call-arg]
                _env_file=None,
                opening_enabled=False,
                database_url=postgres.paths.database_url,
                credential_service_name="com.poly.desktop.integrations",
                local_setup_enabled=True,
            )
            desktop_session = DesktopSession.create()
            bootstrap_path = desktop_session.bootstrap_path
            application_result = self.application_factory(
                configured_settings, desktop_session
            )
            if inspect.isawaitable(application_result):
                container, application = await application_result
            else:
                container, application = application_result
            self.container = container
            if marker_existed:
                await container.system_control.disable_opening_async(
                    UNCLEAN_SHUTDOWN_REASON
                )
            self.server = self.server_factory(application)
            desktop_session.bind_port(self.server.port)
            await self.server.start()
            self._ready = True
            ready = RuntimeEvent(
                RuntimeState.READY,
                {"port": self.server.port, "bootstrap_path": bootstrap_path},
            )
            await self._emit(ready)
            return ready
        except asyncio.CancelledError:
            await self._finish_cleanup(clean=False, disable_reason=None)
            raise
        except Exception:  # noqa: BLE001 - sanitize every orchestration boundary
            await self._finish_cleanup(clean=False, disable_reason=None)
            if phase is RuntimeState.PREPARING_DATABASE:
                return await self._failure(
                    "database_unavailable", "private database is unavailable"
                )
            if phase is RuntimeState.MIGRATING:
                return await self._failure(
                    "migration_failed", "database migration failed"
                )
            return await self._failure(
                "runtime_unavailable", "desktop services could not start"
            )

    async def stop(self, reason: str = "application quit") -> RuntimeEvent:
        if self._terminal_event is not None:
            return self._terminal_event
        if not self._start_called:
            return RuntimeEvent(RuntimeState.STOPPED, {})
        if not self._ready:
            return RuntimeEvent(RuntimeState.STOPPED, {})

        try:
            await self._emit(RuntimeEvent(RuntimeState.SHUTTING_DOWN, {}))
        except asyncio.CancelledError:
            await self._finish_cleanup(clean=True, disable_reason=reason)
            raise
        except Exception:  # noqa: BLE001 - output failure cannot block cleanup
            await self._finish_cleanup(clean=True, disable_reason=reason)
            return await self._failure("shutdown_failed", "runtime shutdown failed")
        clean = await self._finish_cleanup(clean=True, disable_reason=reason)
        if not clean:
            return await self._failure("shutdown_failed", "runtime shutdown failed")
        stopped = RuntimeEvent(RuntimeState.STOPPED, {})
        self._terminal_event = stopped
        await self._emit(stopped)
        return stopped

    async def wait_for_failure(self) -> RuntimeEvent:
        """Wait for an owned service to stop outside the normal shutdown path."""
        if self._terminal_event is not None:
            return self._terminal_event
        if not self._ready or self.postgres is None or self.server is None:
            return await self._failure(
                "runtime_unavailable", "desktop runtime is not ready"
            )

        watchers = {
            asyncio.create_task(self.postgres.wait()),
            asyncio.create_task(self.server.wait()),
        }
        finalize_task: asyncio.Task[RuntimeEvent] | None = None
        try:
            done, _pending = await asyncio.wait(
                watchers, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    task.result()
                except Exception as service_error:  # noqa: BLE001
                    del service_error

            # Latch the terminal outcome without yielding. A simultaneous parent
            # shutdown must never overwrite an observed service crash with STOPPED.
            self._ready = False
            failed = RuntimeEvent(
                RuntimeState.FAILED,
                {
                    "code": "runtime_unavailable",
                    "detail": "desktop service stopped unexpectedly",
                },
            )
            self._terminal_event = failed
            finalize_task = asyncio.create_task(
                self._finalize_unexpected_failure(failed)
            )
            try:
                return await asyncio.shield(finalize_task)
            except asyncio.CancelledError:
                while not finalize_task.done():
                    try:
                        await asyncio.shield(finalize_task)
                    except asyncio.CancelledError:
                        continue
                raise
        finally:
            for task in watchers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)

    async def _finalize_unexpected_failure(
        self, failed: RuntimeEvent
    ) -> RuntimeEvent:
        await self._finish_cleanup(clean=False, disable_reason=None)
        try:
            await self._emit(failed)
        except Exception as emit_error:  # noqa: BLE001 - cleanup still owns resources
            del emit_error
        return failed

    async def _finish_cleanup(self, *, clean: bool, disable_reason: str | None) -> bool:
        cleanup_task = self._cleanup_task
        if cleanup_task is None:
            cleanup_task = asyncio.create_task(
                self._cleanup(clean=clean, disable_reason=disable_reason)
            )
            self._cleanup_task = cleanup_task
        try:
            return await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    continue
            raise

    async def _cleanup(self, *, clean: bool, disable_reason: str | None) -> bool:
        succeeded = True
        if disable_reason is not None and self.container is not None:
            try:
                await self.container.system_control.disable_opening_async(
                    disable_reason
                )
            except Exception:  # noqa: BLE001 - continue owned cleanup
                succeeded = False

        if self.server is not None:
            try:
                await self.server.stop()
            except Exception:  # noqa: BLE001 - continue owned cleanup
                succeeded = False
        elif self.container is not None:
            try:
                await self.container.close()
            except Exception:  # noqa: BLE001 - continue owned cleanup
                succeeded = False

        if self.postgres is not None:
            try:
                await self.postgres.stop()
            except Exception:  # noqa: BLE001 - preserve the recovery marker
                succeeded = False

        if clean and succeeded and self._marker_path is not None:
            try:
                self.marker_filesystem.remove(self._marker_path)
            except Exception:  # noqa: BLE001 - a remaining marker is fail-safe
                succeeded = False
        return succeeded

    async def _emit(self, event: RuntimeEvent) -> None:
        if self.event_sink is None:
            return
        result = self.event_sink(event)
        if inspect.isawaitable(result):
            await result

    async def _failure(
        self, code: str, detail: str, *, terminal: bool = True
    ) -> RuntimeEvent:
        failed = RuntimeEvent(RuntimeState.FAILED, {"code": code, "detail": detail})
        if terminal:
            self._terminal_event = failed
        await self._emit(failed)
        return failed

    def _create_postgres(self, start_command: StartCommand) -> PostgresManager:
        data_dir = self._path_mapper(start_command.data_dir)
        runtime_dir = self._path_mapper(start_command.runtime_dir)
        paths = PostgresPaths(
            bin_dir=self.project_root / "postgres" / "bin",
            data_dir=data_dir / "postgres",
            socket_dir=runtime_dir / "postgres",
            log_file=data_dir / "postgres.log",
        )
        return PostgresManager(paths)

    async def _create_application(
        self,
        configured_settings: Settings,
        desktop_session: DesktopSession,
    ) -> tuple[ApplicationContainer, FastAPI]:
        from backend.app.main import create_app

        container = ApplicationContainer.runtime(
            configured_settings=configured_settings
        )
        try:
            application = create_app(
                container,
                allow_local_setup=True,
                desktop_session=desktop_session,
                configured_settings=configured_settings,
                manage_runtime_lifespan=True,
                static_dir=self.project_root / "frontend" / "dist",
            )
        except Exception:
            try:
                await container.close()
            except Exception as cleanup_error:  # noqa: BLE001
                del cleanup_error  # Preserve the application creation failure.
            raise
        return container, application
