from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

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


async def _read_line(line_reader: LineReader | None) -> bytes:
    if line_reader is None:
        return await asyncio.to_thread(sys.stdin.buffer.readline)
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

    first_line = await _read_line(line_reader)
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
        line = await _read_line(line_reader)
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
    required = [
        root / "frontend" / "dist" / "index.html",
        root / "alembic.ini",
        root / "migrations" / "env.py",
        *(
            root / "postgres" / "bin" / name
            for name in (
                "initdb",
                "postgres",
                "pg_isready",
                "psql",
                "createdb",
            )
        ),
    ]
    shared_libraries = (root / "postgres" / "lib").rglob("*.dylib")
    if any(not path.is_file() for path in required) or not any(
        path.is_file() for path in shared_libraries
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
