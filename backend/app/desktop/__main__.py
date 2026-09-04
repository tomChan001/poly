from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import stat
import sys
import tempfile
import threading
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol

from backend.app.desktop.protocol import (
    RuntimeEvent,
    RuntimeState,
    ShutdownCommand,
    StartCommand,
    parse_command,
)
from backend.app.desktop.runtime import DesktopRuntime, packaged_project_root

LineReader = Callable[[], bytes | Awaitable[bytes]]
EventWriter = Callable[[str], None | Awaitable[None]]


class _BinaryLineStream(Protocol):
    def readline(self) -> bytes: ...


class _StdinLeaseReader:
    """Move blocking stdin reads onto a process-safe daemon thread."""

    def __init__(self, stream: _BinaryLineStream) -> None:
        self._stream = stream
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._started = False
        self._eof_queued = False

    async def readline(self) -> bytes:
        if not self._started:
            self._started = True
            threading.Thread(
                target=self._pump,
                name="poly-stdin-lease",
                daemon=True,
            ).start()
        return await self._queue.get()

    def _pump(self) -> None:
        while True:
            try:
                line = self._stream.readline()
            except Exception:  # noqa: BLE001 - a broken lease is equivalent to EOF
                line = b""
            try:
                self._loop.call_soon_threadsafe(self._enqueue, line)
            except RuntimeError:
                return
            if not line:
                return

    def _enqueue(self, line: bytes) -> None:
        if self._eof_queued:
            return
        self._queue.put_nowait(line)
        if not line:
            self._eof_queued = True


async def _read_line(
    line_reader: LineReader | None,
    stdin_reader: _StdinLeaseReader | None = None,
) -> bytes:
    if line_reader is None:
        active_reader = stdin_reader or _StdinLeaseReader(sys.stdin.buffer)
        return await active_reader.readline()
    result = line_reader()
    if inspect.isawaitable(result):
        return await result
    return result


async def _write_line(event_writer: EventWriter, line: str) -> None:
    result = event_writer(line)
    if inspect.isawaitable(result):
        await result


def _stdout_writer(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _failure(code: str, detail: str) -> RuntimeEvent:
    return RuntimeEvent(RuntimeState.FAILED, {"code": code, "detail": detail})


async def run_stdio(
    *,
    runtime: DesktopRuntime | None = None,
    line_reader: LineReader | None = None,
    event_writer: EventWriter = _stdout_writer,
) -> int:
    async def emit(event: RuntimeEvent) -> None:
        await _write_line(event_writer, event.to_json())

    stdin_reader = _StdinLeaseReader(sys.stdin.buffer) if line_reader is None else None
    first_line = await _read_line(line_reader, stdin_reader)
    try:
        first = parse_command(first_line.decode("utf-8", errors="strict"))
        if not isinstance(first, StartCommand):
            raise TypeError("first command must start runtime")
    except (TypeError, UnicodeError, ValueError):
        await emit(_failure("invalid_start_command", "invalid desktop command"))
        return 2

    active_runtime = runtime or DesktopRuntime(event_sink=emit)
    try:
        result = await active_runtime.start(first)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - never expose command or credential values
        await emit(_failure("runtime_unavailable", "desktop runtime failed"))
        try:
            await active_runtime.stop("runtime failure")
        except Exception as cleanup_error:  # noqa: BLE001
            del cleanup_error  # The emitted failure is already stable.
        return 1
    if result.state is RuntimeState.FAILED:
        return 1

    while True:
        line = await _read_line(line_reader, stdin_reader)
        if not line:
            stopped = await active_runtime.stop("parent process ended")
            return 0 if stopped.state is RuntimeState.STOPPED else 1
        try:
            command = parse_command(line.decode("utf-8", errors="strict"))
            if not isinstance(command, ShutdownCommand):
                raise TypeError("runtime already started")
        except (TypeError, UnicodeError, ValueError):
            await active_runtime.stop("invalid parent command")
            await emit(_failure("invalid_start_command", "invalid desktop command"))
            return 2
        stopped = await active_runtime.stop(command.reason)
        return 0 if stopped.state is RuntimeState.STOPPED else 1


def self_test(
    *, project_root: Path | None = None, temp_root: Path | None = None
) -> RuntimeEvent:
    root = (project_root or packaged_project_root()).resolve()
    required_files = [
        (root / "frontend" / "dist" / "index.html", root / "frontend" / "dist"),
        (root / "alembic.ini", root),
        (root / "migrations" / "env.py", root / "migrations"),
    ]
    postgres_bin = root / "postgres" / "bin"
    required_executables = [
        (postgres_bin / name, postgres_bin)
        for name in ("initdb", "postgres", "pg_isready", "psql", "createdb")
    ]
    library_root = root / "postgres" / "lib"
    try:
        shared_libraries = list(library_root.rglob("*.dylib"))
    except OSError:
        shared_libraries = []
    if (
        any(
            not _is_safe_packaged_file(path, boundary)
            for path, boundary in required_files
        )
        or any(
            not _is_safe_packaged_executable(path, boundary)
            for path, boundary in required_executables
        )
        or not shared_libraries
        or any(
            not _is_safe_packaged_file(path, library_root) for path in shared_libraries
        )
    ):
        return _failure("resource_missing", "required packaged resource is unavailable")
    try:
        with tempfile.TemporaryDirectory(dir=temp_root) as directory:
            probe = Path(directory) / "write-probe"
            probe.write_bytes(b"ok")
            probe.unlink()
    except OSError:
        return _failure("runtime_unavailable", "temporary directory is not writable")
    return RuntimeEvent(RuntimeState.STOPPED, {"self_test": "ok"})


def _is_safe_packaged_file(path: Path, expected_subtree: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        resolved = path.resolve(strict=True)
        metadata = path.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        return False
    return bool(
        resolved.is_relative_to(expected_subtree)
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
    )


def _is_safe_packaged_executable(path: Path, expected_subtree: Path) -> bool:
    return _is_safe_packaged_file(path, expected_subtree) and os.access(path, os.X_OK)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="poly-runtime")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        event = self_test()
        _stdout_writer(event.to_json())
        return 0 if event.state is RuntimeState.STOPPED else 1
    return asyncio.run(run_stdio())


if __name__ == "__main__":
    raise SystemExit(main())
