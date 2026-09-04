import asyncio
import socket
import sys
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.app.container import ApplicationContainer
from backend.app.core.config import settings
from backend.app.desktop import server as server_module
from backend.app.desktop.server import (
    LoopbackServer,
    LoopbackStartupError,
    bind_loopback_socket,
)
from backend.app.desktop.session import DesktopSession
from backend.app.main import create_app


def test_server_socket_is_ipv4_loopback_with_os_selected_port() -> None:
    sock = bind_loopback_socket()
    try:
        host, port = sock.getsockname()
        assert sock.family == socket.AF_INET
        assert host == "127.0.0.1"
        assert 0 < port < 65536
    finally:
        sock.close()


def test_closing_server_socket_releases_the_listener() -> None:
    sock = bind_loopback_socket()
    host, port = sock.getsockname()
    sock.close()

    replacement = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        replacement.bind((host, port))
    finally:
        replacement.close()


def test_live_server_socket_does_not_allow_a_second_listener() -> None:
    sock = bind_loopback_socket()
    competitor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
        if sys.platform == "win32":
            competitor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            competitor.bind(sock.getsockname())
            competitor.listen(socket.SOMAXCONN)
    finally:
        competitor.close()
        sock.close()


@pytest.mark.asyncio
async def test_packaged_frontend_is_served_after_api_routes(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        "<main>Poly desktop</main>", encoding="utf-8"
    )
    app = create_app(ApplicationContainer(), static_dir=tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/")
        health = await client.get("/health")

    assert "Poly desktop" in page.text
    assert health.json()["status"] == "ok"


def test_static_directory_requires_an_index_file(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError, match="desktop static directory has no index.html"
    ):
        create_app(ApplicationContainer(), static_dir=tmp_path)


def test_static_index_validation_uses_staticfiles_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "index.html").write_text("outside boundary", encoding="utf-8")
    lookup_called = False

    def reject_index(
        _static_files: StaticFiles, path: str
    ) -> tuple[str, None]:
        nonlocal lookup_called
        assert path == "index.html"
        lookup_called = True
        return "", None

    monkeypatch.setattr(StaticFiles, "lookup_path", reject_index)

    with pytest.raises(
        ValueError, match="desktop static directory has no index.html"
    ):
        create_app(ApplicationContainer(), static_dir=tmp_path)

    assert lookup_called is True


def test_static_index_symlink_cannot_escape_static_directory(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    outside_index = tmp_path / "outside-index.html"
    outside_index.write_text("outside boundary", encoding="utf-8")
    try:
        (static_dir / "index.html").symlink_to(outside_index)
    except OSError as exc:
        if sys.platform == "win32" and exc.winerror == 1314:
            pytest.skip("creating symlinks requires Windows developer privileges")
        raise

    with pytest.raises(
        ValueError, match="desktop static directory has no index.html"
    ):
        create_app(ApplicationContainer(), static_dir=static_dir)


@pytest.mark.asyncio
async def test_static_mount_does_not_swallow_registered_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    session = DesktopSession.create()
    bootstrap_path = session.bootstrap_path

    (tmp_path / "index.html").write_text("desktop root", encoding="utf-8")
    for route_path in ("api/opportunities", "health", bootstrap_path.lstrip("/")):
        route_directory = tmp_path / route_path
        route_directory.mkdir(parents=True)
        (route_directory / "index.html").write_text(
            f"static marker for {route_path}", encoding="utf-8"
        )

    app = create_app(
        ApplicationContainer(),
        allow_local_setup=True,
        desktop_session=session,
        static_dir=tmp_path,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        bootstrap = await client.get(bootstrap_path, follow_redirects=False)
        opportunities = await client.get("/api/opportunities")
        health = await client.get("/health")

    assert bootstrap.status_code == 303
    assert opportunities.status_code == 200
    assert opportunities.headers["content-type"].startswith("application/json")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


class _RecordingUvicornServer:
    def __init__(self, _config: object) -> None:
        self.started = False
        self.should_exit = False
        self.sockets: list[socket.socket] | None = None

    async def serve(self, *, sockets: list[socket.socket]) -> None:
        self.sockets = sockets
        self.started = True
        while not self.should_exit:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_loopback_server_passes_its_prebound_socket_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module.uvicorn, "Server", _RecordingUvicornServer)
    subject = LoopbackServer(FastAPI())
    owned_socket = subject.socket

    await subject.start()
    try:
        recording_server = cast(_RecordingUvicornServer, subject.server)
        assert recording_server.sockets == [owned_socket]
        assert subject.port == owned_socket.getsockname()[1]
    finally:
        await subject.stop()

    assert owned_socket.fileno() == -1


class _StalledUvicornServer:
    def __init__(self, _config: object) -> None:
        self.started = False
        self.should_exit = False
        self.cancelled = False

    async def serve(self, *, sockets: list[socket.socket]) -> None:
        del sockets
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _SystemExitUvicornServer:
    def __init__(self, _config: object) -> None:
        self.started = False
        self.should_exit = False

    async def serve(self, *, sockets: list[socket.socket]) -> None:
        del sockets
        raise SystemExit(3)


@pytest.mark.asyncio
async def test_system_exit_during_start_is_observed_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module.uvicorn, "Server", _SystemExitUvicornServer)
    subject = LoopbackServer(FastAPI())
    owned_socket = subject.socket

    with pytest.raises(LoopbackStartupError) as raised:
        await subject.start()

    assert isinstance(raised.value.__cause__, SystemExit)
    assert raised.value.__cause__.code == 3
    assert subject._task is not None
    assert subject._task.done()
    assert subject._task.exception() is raised.value
    assert owned_socket.fileno() == -1
    assert subject.server.should_exit is True
    assert subject._stopped is True


@pytest.mark.asyncio
async def test_start_timeout_cancels_task_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module.uvicorn, "Server", _StalledUvicornServer)
    monkeypatch.setattr(server_module, "STARTUP_TIMEOUT_SECONDS", 0.01)
    subject = LoopbackServer(FastAPI())
    owned_socket = subject.socket

    with pytest.raises(TimeoutError, match="loopback server did not start"):
        await asyncio.wait_for(subject.start(), timeout=0.25)

    stalled_server = cast(_StalledUvicornServer, subject.server)
    assert stalled_server.cancelled is True
    assert owned_socket.fileno() == -1


@pytest.mark.asyncio
async def test_external_start_cancellation_is_not_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module.uvicorn, "Server", _StalledUvicornServer)
    subject = LoopbackServer(FastAPI())
    start_task = asyncio.create_task(subject.start())
    while subject._task is None:
        await asyncio.sleep(0)

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    stalled_server = cast(_StalledUvicornServer, subject.server)
    assert stalled_server.cancelled is True
    assert subject.socket.fileno() == -1
    assert subject._stopped is True


class _FailingOnStopUvicornServer(_RecordingUvicornServer):
    async def serve(self, *, sockets: list[socket.socket]) -> None:
        self.sockets = sockets
        self.started = True
        while not self.should_exit:
            await asyncio.sleep(0)
        raise RuntimeError("uvicorn shutdown failed")


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_closes_resources_when_uvicorn_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module.uvicorn, "Server", _FailingOnStopUvicornServer)
    subject = LoopbackServer(FastAPI())
    owned_socket = subject.socket

    await subject.start()
    with pytest.raises(RuntimeError, match="uvicorn shutdown failed"):
        await subject.stop()
    await subject.stop()

    assert owned_socket.fileno() == -1


@pytest.mark.asyncio
async def test_stop_before_start_is_safe_and_prevents_later_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module.uvicorn, "Server", _RecordingUvicornServer)
    subject = LoopbackServer(FastAPI())

    await subject.stop()
    await subject.stop()

    assert subject.socket.fileno() == -1
    with pytest.raises(RuntimeError, match="already stopped"):
        await subject.start()


@pytest.mark.asyncio
async def test_start_is_idempotent_while_server_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module.uvicorn, "Server", _RecordingUvicornServer)
    subject = LoopbackServer(FastAPI())

    await subject.start()
    first_task: asyncio.Task[Any] | None = subject._task
    await subject.start()

    assert subject._task is first_task
    await subject.stop()
