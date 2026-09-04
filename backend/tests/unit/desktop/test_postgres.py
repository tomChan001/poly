import asyncio
import sys
import traceback
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pytest

from backend.app.desktop.postgres import (
    SHUTDOWN_TIMEOUT_SECONDS,
    AsyncioSubprocessRunner,
    CompletedResult,
    PostgresManager,
    PostgresPaths,
    PostgresRuntimeError,
    build_createdb_command,
    build_initdb_command,
    build_pg_isready_command,
    build_postgres_command,
    build_psql_database_exists_command,
)


@dataclass(frozen=True, slots=True)
class _Result(CompletedResult):
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _Child:
    def __init__(
        self,
        *,
        returncode: int | None = None,
        stall_once: bool = False,
        fail_wait_once: bool = False,
    ) -> None:
        self.returncode = returncode
        self.stall_once = stall_once
        self.fail_wait_once = fail_wait_once
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.wait_started = asyncio.Event()
        self.release_wait = asyncio.Event()

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        self.wait_started.set()
        if self.fail_wait_once and self.wait_calls == 1:
            await self.release_wait.wait()
            raise RuntimeError("cleanup failed")
        if self.stall_once and self.wait_calls == 1:
            await asyncio.Event().wait()
        self.returncode = 0
        return self.returncode


class _Runner:
    def __init__(
        self,
        results: Sequence[_Result | BaseException],
        *,
        child: _Child | None = None,
        spawn_error: BaseException | None = None,
    ) -> None:
        self.results = deque(results)
        self.child = child or _Child()
        self.spawn_error = spawn_error
        self.calls: list[tuple[str, list[str], object]] = []

    async def run(self, argv: list[str], *, timeout: float | None = None) -> _Result:
        self.calls.append(("run", argv, timeout))
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    async def spawn(self, argv: list[str], *, log_file: Path) -> _Child:
        self.calls.append(("spawn", argv, log_file))
        if self.spawn_error is not None:
            raise self.spawn_error
        return self.child


class _FileSystem:
    def __init__(self, *, symlinks: set[Path] | None = None) -> None:
        self.symlinks = symlinks or set()
        self.existing: set[Path] = set()
        self.mkdirs: list[tuple[Path, bool, bool]] = []
        self.chmods: list[tuple[Path, int]] = []

    def is_symlink(self, path: Path) -> bool:
        return path in self.symlinks

    def exists(self, path: Path) -> bool:
        return path in self.existing

    def mkdir(self, path: Path, *, parents: bool, exist_ok: bool) -> None:
        self.mkdirs.append((path, parents, exist_ok))
        self.existing.add(path)

    def chmod(self, path: Path, mode: int) -> None:
        self.chmods.append((path, mode))


def _successful_runner(*, database_exists: bool = True) -> _Runner:
    return _Runner(
        [
            _Result(),
            _Result(),
            _Result(stdout="1\n" if database_exists else ""),
            *([] if database_exists else [_Result()]),
        ]
    )


def test_initdb_rejects_network_auth_and_uses_local_role(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    assert build_initdb_command(paths) == [
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


def test_postgres_has_no_tcp_listener_and_private_socket(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    command = build_postgres_command(paths)
    assert "listen_addresses=" in command
    assert f"unix_socket_directories={paths.socket_dir}" in command
    assert "unix_socket_permissions=0700" in command
    assert not any("0.0.0.0" in value or "::" in value for value in command)


def test_database_url_uses_encoded_unix_socket_and_poly_database(
    tmp_path: Path,
) -> None:
    paths = PostgresPaths.for_test(tmp_path / "path with spaces")
    encoded_socket = quote(str(paths.socket_dir), safe="")

    assert paths.database_url == (
        f"postgresql+asyncpg://poly@/poly?host={encoded_socket}&port=5432"
    )


def test_manager_can_be_constructed_before_an_event_loop(tmp_path: Path) -> None:
    PostgresManager(PostgresPaths.for_test(tmp_path), runner=_Runner([]))


def test_client_command_builders_use_only_the_private_socket(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)

    assert build_pg_isready_command(paths) == [
        str(paths.bin_dir / "pg_isready"),
        "-h",
        str(paths.socket_dir),
        "-p",
        "5432",
        "-U",
        "poly",
        "-d",
        "postgres",
        "-q",
    ]
    assert build_psql_database_exists_command(paths) == [
        str(paths.bin_dir / "psql"),
        "-h",
        str(paths.socket_dir),
        "-p",
        "5432",
        "-U",
        "poly",
        "-d",
        "postgres",
        "-t",
        "-A",
        "-c",
        "SELECT 1 FROM pg_database WHERE datname='poly'",
    ]
    assert build_createdb_command(paths) == [
        str(paths.bin_dir / "createdb"),
        "-h",
        str(paths.socket_dir),
        "-p",
        "5432",
        "-U",
        "poly",
        "poly",
    ]


@pytest.mark.asyncio
async def test_asyncio_runner_captures_output_and_exit_code() -> None:
    runner = AsyncioSubprocessRunner()

    result = await runner.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; print('runner stdout'); "
                "print('runner stderr', file=sys.stderr); sys.exit(7)"
            ),
        ],
        timeout=2,
    )

    assert result.returncode == 7
    assert result.stdout.strip() == "runner stdout"
    assert result.stderr.strip() == "runner stderr"


@pytest.mark.asyncio
async def test_asyncio_runner_enforces_run_timeout() -> None:
    runner = AsyncioSubprocessRunner()
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            runner.run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                timeout=0.03,
            ),
            timeout=1,
        )
    assert loop.time() - started_at < 0.5


@pytest.mark.asyncio
async def test_asyncio_runner_spawns_terminates_and_waits_for_child(
    tmp_path: Path,
) -> None:
    runner = AsyncioSubprocessRunner()
    child = await runner.spawn(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        log_file=tmp_path / "child.log",
    )

    child.terminate()
    await asyncio.wait_for(child.wait(), timeout=2)

    assert child.returncode is not None
    assert (tmp_path / "child.log").is_file()


@pytest.mark.asyncio
async def test_first_start_runs_commands_in_security_order(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    runner = _successful_runner(database_exists=False)
    subject = PostgresManager(paths, runner=runner)

    await subject.start()

    assert [kind for kind, _, _ in runner.calls] == [
        "run",
        "spawn",
        "run",
        "run",
        "run",
    ]
    assert runner.calls[0][1] == build_initdb_command(paths)
    assert runner.calls[1][1] == build_postgres_command(paths)
    assert runner.calls[2][1] == build_pg_isready_command(paths)
    assert runner.calls[3][1] == build_psql_database_exists_command(paths)
    assert runner.calls[4][1] == build_createdb_command(paths)
    await subject.stop()


@pytest.mark.asyncio
async def test_second_start_skips_initdb_when_pg_version_exists(
    tmp_path: Path,
) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    paths.data_dir.mkdir()
    (paths.data_dir / "PG_VERSION").write_text("16", encoding="ascii")
    runner = _Runner([_Result(), _Result(stdout="1")])
    subject = PostgresManager(paths, runner=runner)

    await subject.start()

    assert [call[1] for call in runner.calls] == [
        build_postgres_command(paths),
        build_pg_isready_command(paths),
        build_psql_database_exists_command(paths),
    ]
    await subject.stop()


@pytest.mark.asyncio
async def test_existing_database_skips_createdb(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    runner = _successful_runner()
    subject = PostgresManager(paths, runner=runner)

    await subject.start()

    assert build_createdb_command(paths) not in [call[1] for call in runner.calls]
    await subject.stop()


@pytest.mark.asyncio
async def test_restart_reaps_exited_owned_child_before_replacement(
    tmp_path: Path,
) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    paths.data_dir.mkdir()
    (paths.data_dir / "PG_VERSION").write_text("16", encoding="ascii")
    first_child = _Child()
    runner = _Runner(
        [_Result(), _Result(stdout="1"), _Result(), _Result(stdout="1")],
        child=first_child,
    )
    subject = PostgresManager(paths, runner=runner)
    await subject.start()
    first_child.returncode = 1
    replacement_child = _Child()
    runner.child = replacement_child

    await subject.start()

    assert first_child.wait_calls == 1
    assert [kind for kind, _, _ in runner.calls].count("spawn") == 2
    await subject.stop()


@pytest.mark.asyncio
async def test_readiness_retries_known_not_ready_codes(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    runner = _Runner(
        [
            _Result(),
            _Result(returncode=1),
            _Result(returncode=2),
            _Result(),
            _Result(stdout="1"),
        ]
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    subject = PostgresManager(
        paths,
        runner=runner,
        startup_timeout=0.1,
        readiness_backoff=0.001,
        sleep=sleep,
    )

    await subject.start()

    readiness_command = build_pg_isready_command(paths)
    assert [call[1] for call in runner.calls].count(readiness_command) == 3
    assert sleeps == [0.001, 0.001]
    await subject.stop()


@pytest.mark.asyncio
async def test_readiness_timeout_is_typed_and_stops_child(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    child = _Child()
    runner = _Runner(
        [_Result(), _Result(returncode=1), _Result(returncode=1)], child=child
    )
    now = 0.0

    def monotonic() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        now += delay

    subject = PostgresManager(
        paths,
        runner=runner,
        startup_timeout=0.015,
        readiness_backoff=0.01,
        monotonic=monotonic,
        sleep=sleep,
    )

    with pytest.raises(PostgresRuntimeError, match="postgres readiness timed out"):
        await subject.start()

    readiness_timeouts = [
        timeout
        for kind, command, timeout in runner.calls
        if kind == "run" and command == build_pg_isready_command(paths)
    ]
    assert readiness_timeouts == pytest.approx([0.015, 0.005])
    assert child.terminate_calls == 1
    assert child.wait_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("results", "spawn_error", "expected_message", "child_was_spawned"),
    [
        ([_Result(returncode=1, stderr="SECRET")], None, "postgres init failed", False),
        ([_Result()], OSError("SECRET"), "postgres start failed", False),
        (
            [_Result(), _Result(returncode=3, stderr="SECRET")],
            None,
            "postgres readiness failed",
            True,
        ),
        (
            [_Result(), _Result(), _Result(returncode=1, stderr="SECRET")],
            None,
            "postgres database check failed",
            True,
        ),
        (
            [
                _Result(),
                _Result(),
                _Result(stdout=""),
                _Result(returncode=1, stderr="SECRET"),
            ],
            None,
            "postgres database creation failed",
            True,
        ),
    ],
)
async def test_start_failures_are_sanitized_and_clean_up_owned_child(
    tmp_path: Path,
    results: list[_Result],
    spawn_error: BaseException | None,
    expected_message: str,
    child_was_spawned: bool,
) -> None:
    child = _Child()
    runner = _Runner(results, child=child, spawn_error=spawn_error)
    subject = PostgresManager(PostgresPaths.for_test(tmp_path), runner=runner)

    with pytest.raises(PostgresRuntimeError, match=expected_message) as raised:
        await subject.start()

    assert "SECRET" not in str(raised.value)
    assert child.terminate_calls == int(child_was_spawned)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("results", "spawn_error", "expected_message"),
    [
        pytest.param(
            [RuntimeError("SECRET from cause")],
            None,
            "postgres init failed",
            id="init",
        ),
        pytest.param(
            [_Result()],
            RuntimeError("SECRET from cause"),
            "postgres start failed",
            id="spawn",
        ),
        pytest.param(
            [_Result(), RuntimeError("SECRET from cause")],
            None,
            "postgres readiness failed",
            id="readiness",
        ),
        pytest.param(
            [_Result(), _Result(), RuntimeError("SECRET from cause")],
            None,
            "postgres database check failed",
            id="query",
        ),
        pytest.param(
            [
                _Result(),
                _Result(),
                _Result(stdout=""),
                RuntimeError("SECRET from cause"),
            ],
            None,
            "postgres database creation failed",
            id="createdb",
        ),
    ],
)
async def test_runner_exception_cause_is_not_exposed(
    tmp_path: Path,
    results: list[_Result | BaseException],
    spawn_error: BaseException | None,
    expected_message: str,
) -> None:
    runner = _Runner(results, spawn_error=spawn_error)
    subject = PostgresManager(PostgresPaths.for_test(tmp_path), runner=runner)

    with pytest.raises(PostgresRuntimeError, match=expected_message) as raised:
        await subject.start()

    formatted = "".join(traceback.format_exception(raised.value))
    assert "SECRET from cause" not in formatted
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_cancellation_after_spawn_cleans_up_and_propagates(
    tmp_path: Path,
) -> None:
    child = _Child()
    runner = _Runner([_Result(), asyncio.CancelledError()], child=child)
    subject = PostgresManager(PostgresPaths.for_test(tmp_path), runner=runner)

    with pytest.raises(asyncio.CancelledError):
        await subject.start()

    assert child.terminate_calls == 1
    assert child.wait_calls == 1


@pytest.mark.asyncio
async def test_start_failure_is_not_overwritten_by_cleanup_failure(
    tmp_path: Path,
) -> None:
    child = _Child(fail_wait_once=True)
    child.release_wait.set()
    runner = _Runner([_Result(), _Result(returncode=3)], child=child)
    subject = PostgresManager(PostgresPaths.for_test(tmp_path), runner=runner)

    with pytest.raises(PostgresRuntimeError, match="postgres readiness failed"):
        await subject.start()

    await subject.stop()
    assert child.terminate_calls == 2
    assert child.wait_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_path_name", ["data", "parent", "socket"])
async def test_start_rejects_symlink_directories_before_use(
    tmp_path: Path, unsafe_path_name: str
) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    unsafe_path = {
        "data": paths.data_dir,
        "parent": paths.data_dir.parent,
        "socket": paths.socket_dir,
    }[unsafe_path_name]
    filesystem = _FileSystem(symlinks={unsafe_path})
    runner = _Runner([])
    subject = PostgresManager(paths, runner=runner, filesystem=filesystem)

    with pytest.raises(PostgresRuntimeError, match="postgres filesystem safety failed"):
        await subject.start()

    assert runner.calls == []
    assert filesystem.mkdirs == []


@pytest.mark.asyncio
async def test_start_creates_private_directories_with_mode_0700(
    tmp_path: Path,
) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    filesystem = _FileSystem()
    runner = _successful_runner()
    subject = PostgresManager(paths, runner=runner, filesystem=filesystem)

    await subject.start()

    assert (paths.data_dir.parent, True, True) in filesystem.mkdirs
    assert (paths.socket_dir, True, True) in filesystem.mkdirs
    assert (paths.data_dir.parent, 0o700) in filesystem.chmods
    assert (paths.socket_dir, 0o700) in filesystem.chmods
    await subject.stop()


@pytest.mark.asyncio
async def test_unexpected_child_exit_during_readiness_fails_without_polling(
    tmp_path: Path,
) -> None:
    child = _Child(returncode=9)
    runner = _Runner([_Result()], child=child)
    subject = PostgresManager(PostgresPaths.for_test(tmp_path), runner=runner)

    with pytest.raises(PostgresRuntimeError, match="postgres exited during readiness"):
        await subject.start()

    assert [kind for kind, _, _ in runner.calls] == ["run", "spawn"]
    assert child.terminate_calls == 0


@pytest.mark.asyncio
async def test_stop_before_start_and_repeated_stop_are_safe(tmp_path: Path) -> None:
    runner = _Runner([])
    subject = PostgresManager(PostgresPaths.for_test(tmp_path), runner=runner)

    await subject.stop()
    await subject.stop()

    assert runner.calls == []


@pytest.mark.asyncio
async def test_stop_terminates_and_waits_for_exact_owned_child(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    owned = _Child()
    unrelated = _Child()
    runner = _successful_runner()
    runner.child = owned
    subject = PostgresManager(paths, runner=runner)

    await subject.start()
    await subject.stop()
    await subject.stop()

    assert SHUTDOWN_TIMEOUT_SECONDS == 10.0
    assert owned.terminate_calls == 1
    assert owned.kill_calls == 0
    assert owned.wait_calls == 1
    assert unrelated.terminate_calls == 0
    assert unrelated.kill_calls == 0
    assert unrelated.wait_calls == 0


@pytest.mark.asyncio
async def test_stop_kills_owned_child_after_graceful_timeout(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    child = _Child(stall_once=True)
    runner = _successful_runner()
    runner.child = child
    subject = PostgresManager(paths, runner=runner, shutdown_timeout=0.001)

    await subject.start()
    await subject.stop()

    assert child.terminate_calls == 1
    assert child.kill_calls == 1
    assert child.wait_calls == 2


@pytest.mark.asyncio
async def test_cancelled_stop_finishes_owned_cleanup_before_propagating(
    tmp_path: Path,
) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    child = _Child(stall_once=True)
    runner = _successful_runner()
    runner.child = child
    subject = PostgresManager(paths, runner=runner, shutdown_timeout=0.03)
    await subject.start()

    stop_task = asyncio.create_task(subject.stop())
    await asyncio.wait_for(child.wait_started.wait(), timeout=0.1)
    stop_task.cancel()
    await asyncio.sleep(0)

    assert stop_task.done() is False
    assert child.terminate_calls == 1
    concurrent_stop = asyncio.create_task(subject.stop())
    await asyncio.sleep(0)
    assert concurrent_stop.done() is False
    with pytest.raises(asyncio.CancelledError):
        await stop_task
    await concurrent_stop
    assert child.kill_calls == 1
    assert child.wait_calls == 2

    await subject.stop()
    assert child.terminate_calls == 1
    assert child.kill_calls == 1
    assert child.wait_calls == 2


@pytest.mark.asyncio
async def test_cancelled_stop_preserves_cancellation_and_failed_child_ownership(
    tmp_path: Path,
) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    child = _Child(fail_wait_once=True)
    runner = _successful_runner()
    runner.child = child
    subject = PostgresManager(paths, runner=runner, shutdown_timeout=1)
    await subject.start()

    stop_task = asyncio.create_task(subject.stop())
    await asyncio.wait_for(child.wait_started.wait(), timeout=0.1)
    stop_task.cancel()
    await asyncio.sleep(0)
    child.release_wait.set()

    with pytest.raises(asyncio.CancelledError):
        await stop_task
    await subject.stop()

    assert child.terminate_calls == 2
    assert child.wait_calls == 2


@pytest.mark.asyncio
async def test_stop_does_not_signal_an_already_exited_child(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    child = _Child()
    runner = _successful_runner()
    runner.child = child
    subject = PostgresManager(paths, runner=runner)

    await subject.start()
    child.returncode = 0
    await subject.stop()

    assert child.terminate_calls == 0
    assert child.kill_calls == 0
    assert child.wait_calls == 1
