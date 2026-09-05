import asyncio
import subprocess
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import quote

POSTGRES_PORT = 5432
STARTUP_TIMEOUT_SECONDS = 10.0
READINESS_BACKOFF_SECONDS = 0.1
COMMAND_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
EXIT_POLL_SECONDS = 0.1

_DATABASE_EXISTS_SQL = "SELECT 1 FROM pg_database WHERE datname='poly'"
_NOT_READY_EXIT_CODES = frozenset({1, 2})


@dataclass(frozen=True, slots=True)
class PostgresPaths:
    bin_dir: Path
    data_dir: Path
    socket_dir: Path
    log_file: Path

    @classmethod
    def for_test(cls, root: Path) -> "PostgresPaths":
        return cls(
            root / "bin",
            root / "data",
            root / "socket",
            root / "postgres.log",
        )

    @property
    def database_url(self) -> str:
        socket = quote(str(self.socket_dir), safe="")
        return f"postgresql+asyncpg://poly@/poly?host={socket}&port=5432"


def build_initdb_command(paths: PostgresPaths) -> list[str]:
    return [
        str(paths.bin_dir / "initdb"),
        "--pgdata",
        str(paths.data_dir),
        "--username",
        "poly",
        "--encoding",
        "UTF8",
        "--auth-local",
        "trust",
        "--auth-host",
        "reject",
        "--no-instructions",
    ]


def build_postgres_command(paths: PostgresPaths) -> list[str]:
    return [
        str(paths.bin_dir / "postgres"),
        "-D",
        str(paths.data_dir),
        "-c",
        "listen_addresses=",
        "-c",
        f"unix_socket_directories={paths.socket_dir}",
        "-c",
        "unix_socket_permissions=0700",
        "-c",
        "port=5432",
        "-c",
        "logging_collector=off",
    ]


def build_pg_isready_command(paths: PostgresPaths) -> list[str]:
    return [
        str(paths.bin_dir / "pg_isready"),
        "-h",
        str(paths.socket_dir),
        "-p",
        str(POSTGRES_PORT),
        "-U",
        "poly",
        "-d",
        "postgres",
        "-q",
    ]


def build_psql_database_exists_command(paths: PostgresPaths) -> list[str]:
    return [
        str(paths.bin_dir / "psql"),
        "-h",
        str(paths.socket_dir),
        "-p",
        str(POSTGRES_PORT),
        "-U",
        "poly",
        "-d",
        "postgres",
        "-t",
        "-A",
        "-c",
        _DATABASE_EXISTS_SQL,
    ]


def build_createdb_command(paths: PostgresPaths) -> list[str]:
    return [
        str(paths.bin_dir / "createdb"),
        "-h",
        str(paths.socket_dir),
        "-p",
        str(POSTGRES_PORT),
        "-U",
        "poly",
        "poly",
    ]


@dataclass(frozen=True, slots=True)
class CompletedResult:
    returncode: int
    stdout: str
    stderr: str


class OwnedChild(Protocol):
    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class Runner(Protocol):
    async def run(
        self, argv: list[str], *, timeout: float | None = None
    ) -> CompletedResult: ...

    async def spawn(self, argv: list[str], *, log_file: Path) -> OwnedChild: ...


class FileSystem(Protocol):
    def is_symlink(self, path: Path) -> bool: ...

    def exists(self, path: Path) -> bool: ...

    def mkdir(self, path: Path, *, parents: bool, exist_ok: bool) -> None: ...

    def chmod(self, path: Path, mode: int) -> None: ...


class _AsyncioOwnedChild:
    def __init__(self, process: asyncio.subprocess.Process, log_stream: BinaryIO):
        self._process = process
        self._log_stream = log_stream

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    async def wait(self) -> int:
        try:
            return await self._process.wait()
        finally:
            self._log_stream.close()


class AsyncioSubprocessRunner:
    async def run(
        self, argv: list[str], *, timeout: float | None = None
    ) -> CompletedResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except BaseException:
            if process.returncode is None:
                process.kill()
                with suppress(BaseException):
                    await process.wait()
            raise
        return CompletedResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    async def spawn(self, argv: list[str], *, log_file: Path) -> _AsyncioOwnedChild:
        log_stream = log_file.open("ab")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
            )
        except BaseException:
            log_stream.close()
            raise
        return _AsyncioOwnedChild(process, log_stream)


class _PathFileSystem:
    def is_symlink(self, path: Path) -> bool:
        return path.is_symlink()

    def exists(self, path: Path) -> bool:
        return path.exists()

    def mkdir(self, path: Path, *, parents: bool, exist_ok: bool) -> None:
        path.mkdir(parents=parents, exist_ok=exist_ok)

    def chmod(self, path: Path, mode: int) -> None:
        path.chmod(mode)


class PostgresRuntimeError(RuntimeError):
    """Raised when the private PostgreSQL runtime cannot be managed safely."""


class PostgresManager:
    def __init__(
        self,
        paths: PostgresPaths,
        *,
        runner: Runner | None = None,
        filesystem: FileSystem | None = None,
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
        readiness_backoff: float = READINESS_BACKOFF_SECONDS,
        command_timeout: float = COMMAND_TIMEOUT_SECONDS,
        shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.paths = paths
        self._runner: Runner = (
            runner if runner is not None else AsyncioSubprocessRunner()
        )
        self._filesystem = filesystem or _PathFileSystem()
        self._startup_timeout = startup_timeout
        self._readiness_backoff = readiness_backoff
        self._command_timeout = command_timeout
        self._shutdown_timeout = shutdown_timeout
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._child: OwnedChild | None = None
        self._cleanup_child: OwnedChild | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._child is not None:
            if self._child.returncode is None:
                return
            await self.stop()

        try:
            self._prepare_directories()
            if not self._filesystem.exists(self.paths.data_dir / "PG_VERSION"):
                await self._require_success(
                    build_initdb_command(self.paths), "postgres init failed"
                )

            try:
                child = await self._runner.spawn(
                    build_postgres_command(self.paths), log_file=self.paths.log_file
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - sanitize the process boundary
                raise PostgresRuntimeError("postgres start failed") from None
            self._child = child

            await self._wait_until_ready(child)
            database = await self._require_success(
                build_psql_database_exists_command(self.paths),
                "postgres database check failed",
            )
            if database.stdout.strip() != "1":
                await self._require_success(
                    build_createdb_command(self.paths),
                    "postgres database creation failed",
                )
        except BaseException:
            try:
                await self.stop()
            except asyncio.CancelledError:
                raise
            except Exception as cleanup_error:  # noqa: BLE001
                del cleanup_error  # Preserve the original startup failure.
            raise

    async def stop(self) -> None:
        child = self._child
        if child is None:
            return

        cleanup_task = self._cleanup_task
        if cleanup_task is None:
            cleanup_task = asyncio.create_task(self._stop_child(child))
            self._cleanup_child = child
            self._cleanup_task = cleanup_task
        elif self._cleanup_child is not child:
            raise PostgresRuntimeError("postgres shutdown ownership conflict")

        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            while not cleanup_task.done():
                with suppress(BaseException):
                    await asyncio.shield(cleanup_task)
            cleanup_succeeded = self._task_succeeded(cleanup_task)
            self._finish_cleanup(child, cleanup_task, cleanup_succeeded)
            raise
        except BaseException:
            self._finish_cleanup(child, cleanup_task, succeeded=False)
            raise
        else:
            self._finish_cleanup(child, cleanup_task, succeeded=True)

    async def wait(self) -> None:
        child = self._child
        if child is None:
            raise PostgresRuntimeError("postgres is not running")
        while child.returncode is None:
            await asyncio.sleep(EXIT_POLL_SECONDS)

    async def _stop_child(self, child: OwnedChild) -> None:
        if child.returncode is not None:
            await child.wait()
            return

        child.terminate()
        try:
            await asyncio.wait_for(child.wait(), timeout=self._shutdown_timeout)
        except TimeoutError:
            child.kill()
            await child.wait()

    @staticmethod
    def _task_succeeded(task: asyncio.Task[None]) -> bool:
        try:
            task.result()
        except BaseException:  # noqa: BLE001 - inspect the task without leaking it
            return False
        return True

    def _finish_cleanup(
        self,
        child: OwnedChild,
        cleanup_task: asyncio.Task[None],
        succeeded: bool,
    ) -> None:
        if self._cleanup_task is not cleanup_task:
            return
        self._cleanup_task = None
        self._cleanup_child = None
        if succeeded and self._child is child:
            self._child = None

    def _prepare_directories(self) -> None:
        try:
            self._reject_symlink_directories()
            for directory in (self.paths.data_dir.parent, self.paths.socket_dir):
                self._filesystem.mkdir(directory, parents=True, exist_ok=True)
            self._reject_symlink_directories()
            for directory in (self.paths.data_dir.parent, self.paths.socket_dir):
                self._filesystem.chmod(directory, 0o700)
        except PostgresRuntimeError:
            raise
        except Exception:  # noqa: BLE001 - sanitize the filesystem boundary
            raise PostgresRuntimeError("postgres filesystem setup failed") from None

    def _reject_symlink_directories(self) -> None:
        checked_paths = (
            self.paths.data_dir,
            self.paths.data_dir.parent,
            self.paths.socket_dir,
        )
        if any(self._filesystem.is_symlink(path) for path in checked_paths):
            raise PostgresRuntimeError("postgres filesystem safety failed")

    async def _require_success(
        self, command: list[str], error_message: str
    ) -> CompletedResult:
        try:
            result = await self._runner.run(command, timeout=self._command_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - sanitize the process boundary
            raise PostgresRuntimeError(error_message) from None
        if result.returncode != 0:
            raise PostgresRuntimeError(error_message)
        return result

    async def _wait_until_ready(self, child: OwnedChild) -> None:
        deadline = self._monotonic() + self._startup_timeout
        while True:
            if child.returncode is not None:
                raise PostgresRuntimeError("postgres exited during readiness")
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise PostgresRuntimeError("postgres readiness timed out")

            try:
                result = await self._runner.run(
                    build_pg_isready_command(self.paths),
                    timeout=min(self._command_timeout, remaining),
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - sanitize the process boundary
                raise PostgresRuntimeError("postgres readiness failed") from None

            if child.returncode is not None:
                raise PostgresRuntimeError("postgres exited during readiness")
            if result.returncode == 0:
                return
            if result.returncode not in _NOT_READY_EXIT_CODES:
                raise PostgresRuntimeError("postgres readiness failed")

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise PostgresRuntimeError("postgres readiness timed out")
            await self._sleep(min(self._readiness_backoff, remaining))
