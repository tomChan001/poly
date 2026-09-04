# Poly Local macOS Desktop Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a signed-and-notarization-ready Tauri 2 macOS desktop application that runs the existing React, FastAPI, worker, and PostgreSQL stack locally with no runtime prerequisites.

**Architecture:** Tauri owns the native window and supervises one packaged Python runtime. The Python runtime starts a private Unix-socket-only PostgreSQL instance, applies Alembic migrations, binds FastAPI to an OS-selected `127.0.0.1` port, and reports structured lifecycle events over stdio. The WebView receives a one-time capability Cookie before loading the existing same-origin React UI; existing loopback and Origin checks remain mandatory.

**Tech Stack:** Tauri 2, Rust, React 19, TypeScript, FastAPI, Python 3.12, PyInstaller onedir, PostgreSQL 16, Alembic, pytest, Vitest, Cargo test, GitHub Actions macOS runners.

---

## Delivery order and file map

This is one integrated product rather than three independently shippable features, but it is delivered in three testable milestones:

1. **Local runtime:** Python control protocol, desktop capability, private PostgreSQL, migrations, FastAPI, and safe shutdown.
2. **Desktop shell:** Tauri state machine, process supervision, native status UX, reconnection, and single-instance behavior.
3. **Distribution:** per-architecture PyInstaller/PostgreSQL resources, licenses, signing, notarization, DMG CI, and Mac smoke tests.

Files have these responsibilities:

- `backend/app/desktop/protocol.py`: versioned stdin/stdout messages and redacted serialization.
- `backend/app/desktop/session.py`: one-time bootstrap token and desktop Cookie validation.
- `backend/app/desktop/postgres.py`: PostgreSQL initialization, Unix socket startup, readiness, and shutdown.
- `backend/app/desktop/server.py`: pre-bound loopback socket and Uvicorn lifecycle.
- `backend/app/desktop/runtime.py`: ordered orchestration, migrations, unclean-shutdown marker, and parent-pipe lease.
- `backend/app/desktop/__main__.py`: packaged runtime entry point.
- `backend/app/main.py`: optional desktop session and packaged React static files.
- `backend/app/core/security.py`: retain current checks and add desktop capability as an extra predicate.
- `frontend/src/desktop/`: startup/reconnect/error presentation, isolated from existing business UI.
- `src-tauri/src/runtime/`: Rust protocol, state machine, process adapter, and restart policy.
- `src-tauri/src/app_lifecycle.rs`: single instance and graceful application exit.
- `src-tauri/src/webview.rs`: navigation between bundled status UI and authenticated loopback UI.
- `packaging/macos/`: PyInstaller spec, PostgreSQL assembly, license inventory, signing verification, and smoke scripts.
- `.github/workflows/macos-desktop.yml`: unsigned pull-request checks and signed release builds.

Do not read `.env`. Tests pass explicit settings and mocks. Do not run any AWS operation or real market API call.

### Task 1: Versioned Python control protocol

**Files:**
- Create: `backend/app/desktop/__init__.py`
- Create: `backend/app/desktop/protocol.py`
- Create: `backend/tests/unit/desktop/test_protocol.py`

- [ ] **Step 1: Write failing protocol tests**

```python
import json

import pytest

from backend.app.desktop.protocol import (
    PROTOCOL_VERSION,
    RuntimeEvent,
    RuntimeState,
    StartCommand,
    parse_command,
)


def test_start_command_accepts_only_current_protocol_and_absolute_paths() -> None:
    command = parse_command(json.dumps({
        "version": PROTOCOL_VERSION,
        "command": "start",
        "data_dir": "/Users/operator/Library/Application Support/Poly",
        "runtime_dir": "/private/tmp/poly-123",
        "launch_token": "a" * 43,
    }))

    assert isinstance(command, StartCommand)
    assert command.runtime_dir.is_absolute()


def test_protocol_rejects_relative_paths_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="absolute"):
        parse_command(json.dumps({
            "version": PROTOCOL_VERSION,
            "command": "start",
            "data_dir": "relative",
            "runtime_dir": "/private/tmp/poly-123",
            "launch_token": "a" * 43,
        }))


def test_event_serialization_never_contains_launch_token() -> None:
    event = RuntimeEvent(RuntimeState.READY, {"port": 49152})

    encoded = event.to_json()

    assert json.loads(encoded) == {
        "version": PROTOCOL_VERSION,
        "state": "ready",
        "port": 49152,
    }
    assert "launch_token" not in encoded
```

- [ ] **Step 2: Run the tests and confirm the module is missing**

Run: `uv run pytest backend/tests/unit/desktop/test_protocol.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'backend.app.desktop'`.

- [ ] **Step 3: Add the strict protocol model**

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1


class RuntimeState(StrEnum):
    INITIALIZING = "initializing"
    PREPARING_DATABASE = "preparing_database"
    MIGRATING = "migrating"
    STARTING_SERVICES = "starting_services"
    READY = "ready"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StartCommand:
    data_dir: Path
    runtime_dir: Path
    launch_token: str


@dataclass(frozen=True, slots=True)
class ShutdownCommand:
    reason: str = "desktop requested shutdown"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    state: RuntimeState
    fields: dict[str, object]

    def to_json(self) -> str:
        return json.dumps(
            {"version": PROTOCOL_VERSION, "state": self.state, **self.fields},
            separators=(",", ":"),
        )


def parse_command(line: str) -> StartCommand | ShutdownCommand:
    payload: dict[str, Any] = json.loads(line)
    if payload.get("version") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    command = payload.get("command")
    if command == "shutdown":
        allowed = {"version", "command", "reason"}
        if set(payload) - allowed:
            raise ValueError("unknown shutdown command field")
        return ShutdownCommand(str(payload.get("reason", "desktop requested shutdown")))
    if command != "start":
        raise ValueError("unsupported command")
    allowed = {"version", "command", "data_dir", "runtime_dir", "launch_token"}
    if set(payload) != allowed:
        raise ValueError("invalid start command fields")
    data_dir = Path(str(payload["data_dir"]))
    runtime_dir = Path(str(payload["runtime_dir"]))
    if not data_dir.is_absolute() or not runtime_dir.is_absolute():
        raise ValueError("desktop paths must be absolute")
    token = str(payload["launch_token"])
    if len(token) < 43:
        raise ValueError("launch token is too short")
    return StartCommand(data_dir, runtime_dir, token)
```

Create an empty `backend/app/desktop/__init__.py`.

- [ ] **Step 4: Run protocol tests**

Run: `uv run pytest backend/tests/unit/desktop/test_protocol.py -v`

Expected: 3 passed.

- [ ] **Step 5: Commit the protocol boundary**

```bash
git add backend/app/desktop backend/tests/unit/desktop/test_protocol.py
git commit -m "feat: add desktop runtime protocol"
```

### Task 2: One-time WebView capability without weakening loopback security

**Files:**
- Create: `backend/app/desktop/session.py`
- Create: `backend/tests/integration/api/test_desktop_session.py`
- Modify: `backend/app/core/security.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write failing security tests**

```python
import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.config import settings
from backend.app.desktop.session import DesktopSession
from backend.app.main import create_app


@pytest.mark.asyncio
async def test_desktop_api_requires_capability_cookie(monkeypatch) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    session = DesktopSession.create()
    app = create_app(
        ApplicationContainer(),
        allow_local_setup=True,
        desktop_session=session,
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        denied = await client.get("/api/opportunities")
        denied_health = await client.get("/health")
        bootstrap = await client.get(session.bootstrap_path, follow_redirects=False)
        allowed = await client.get(
            "/api/opportunities",
            headers={"Origin": "http://127.0.0.1:49152"},
        )
        replay = await client.get(session.bootstrap_path, follow_redirects=False)

    assert denied.status_code == 403
    assert denied_health.status_code == 403
    assert bootstrap.status_code == 303
    assert bootstrap.headers["location"] == "/"
    assert "HttpOnly" in bootstrap.headers["set-cookie"]
    assert "SameSite=strict" in bootstrap.headers["set-cookie"]
    assert allowed.status_code == 200
    assert replay.status_code == 403


@pytest.mark.asyncio
async def test_hostile_origin_stays_blocked_after_bootstrap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    session = DesktopSession.create()
    app = create_app(ApplicationContainer(), allow_local_setup=True, desktop_session=session)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        await client.get(session.bootstrap_path)
        response = await client.get(
            "/api/opportunities",
            headers={"Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "local access only"}
```

- [ ] **Step 2: Verify tests fail because desktop sessions do not exist**

Run: `uv run pytest backend/tests/integration/api/test_desktop_session.py -v`

Expected: FAIL during collection importing `DesktopSession`.

- [ ] **Step 3: Add the one-time session implementation**

```python
import secrets
from dataclasses import dataclass, field
from hmac import compare_digest

from fastapi import Request

COOKIE_NAME = "poly_desktop_session"


@dataclass(slots=True)
class DesktopSession:
    _bootstrap_token: str
    _cookie_value: str
    _bootstrap_used: bool = field(default=False, init=False)

    @classmethod
    def create(cls) -> "DesktopSession":
        return cls(secrets.token_urlsafe(32), secrets.token_urlsafe(32))

    @property
    def bootstrap_path(self) -> str:
        return f"/desktop/bootstrap/{self._bootstrap_token}"

    def exchange(self, supplied: str) -> str | None:
        if self._bootstrap_used or not compare_digest(supplied, self._bootstrap_token):
            return None
        self._bootstrap_used = True
        self._bootstrap_token = ""
        return self._cookie_value

    def is_authorized(self, request: Request) -> bool:
        supplied = request.cookies.get(COOKIE_NAME, "")
        return bool(supplied) and compare_digest(supplied, self._cookie_value)
```

- [ ] **Step 4: Compose the capability with the existing security checks**

In `get_current_principal`, keep the current `settings.local_setup_enabled`, loopback, trusted Origin, and `allow_local_setup` conditions. Immediately before returning `Principal`, add:

```python
        desktop_session = getattr(request.app.state, "desktop_session", None)
        if desktop_session is not None and not desktop_session.is_authorized(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="desktop session required",
            )
```

Extend `create_app` with `desktop_session: DesktopSession | None = None`, store it on `application.state`, and register this route before static files:

```python
    @application.get("/desktop/bootstrap/{token}", include_in_schema=False)
    async def desktop_bootstrap(token: str) -> Response:
        if desktop_session is None:
            raise HTTPException(status_code=404)
        cookie_value = desktop_session.exchange(token)
        if cookie_value is None:
            raise HTTPException(status_code=403, detail="invalid desktop bootstrap")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            cookie_value,
            httponly=True,
            secure=False,
            samesite="strict",
            path="/",
        )
        return response
```

Add the required imports from `fastapi.responses` and `backend.app.desktop.session`. Keep non-desktop construction behavior unchanged.

At the start of the existing `/health` handler, require the same Cookie only when `desktop_session` is present:

```python
        if desktop_session is not None and not desktop_session.is_authorized(request):
            raise HTTPException(status_code=403, detail="desktop session required")
```

Add `request: Request` to the handler signature. Non-desktop health behavior and its existing tests remain unchanged.

- [ ] **Step 5: Run desktop and existing security tests**

Run: `uv run pytest backend/tests/integration/api/test_desktop_session.py backend/tests/integration/api/test_integrations.py backend/tests/integration/api/test_opportunities.py backend/tests/integration/api/test_kill_switch.py -v`

Expected: all selected tests pass, including remote client and hostile Origin rejection.

- [ ] **Step 6: Commit the additive security layer**

```bash
git add backend/app/desktop/session.py backend/app/core/security.py backend/app/main.py backend/tests/integration/api/test_desktop_session.py
git commit -m "feat: require desktop webview capability"
```

### Task 3: Packaged React serving and race-free loopback binding

**Files:**
- Create: `backend/app/desktop/server.py`
- Create: `backend/tests/unit/desktop/test_server.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write the failing socket and static-file tests**

```python
from pathlib import Path

import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.desktop.server import bind_loopback_socket
from backend.app.main import create_app


def test_server_socket_is_ipv4_loopback_with_os_selected_port() -> None:
    sock = bind_loopback_socket()
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1"
        assert 0 < port < 65536
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_packaged_frontend_is_served_after_api_routes(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<main>Poly desktop</main>", encoding="utf-8")
    app = create_app(ApplicationContainer(), static_dir=tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/")
        health = await client.get("/health")

    assert "Poly desktop" in page.text
    assert health.json()["status"] == "ok"
```

- [ ] **Step 2: Confirm the loopback helper and app argument are absent**

Run: `uv run pytest backend/tests/unit/desktop/test_server.py -v`

Expected: FAIL importing `bind_loopback_socket`.

- [ ] **Step 3: Implement socket binding and Uvicorn wrapper**

```python
import asyncio
import socket

import uvicorn
from fastapi import FastAPI


def bind_loopback_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(socket.SOMAXCONN)
    sock.setblocking(False)
    return sock


class LoopbackServer:
    def __init__(self, app: FastAPI) -> None:
        self.socket = bind_loopback_socket()
        self.server = uvicorn.Server(uvicorn.Config(
            app,
            host="127.0.0.1",
            access_log=False,
            log_level="warning",
        ))
        self._task: asyncio.Task[None] | None = None

    @property
    def port(self) -> int:
        return int(self.socket.getsockname()[1])

    async def start(self) -> None:
        self._task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        while not self.server.started:
            if self._task.done():
                await self._task
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        self.server.should_exit = True
        if self._task is not None:
            await self._task
```

- [ ] **Step 4: Mount static files last**

Add `static_dir: Path | None = None` to `create_app`. After all API and bootstrap routes are registered:

```python
    if static_dir is not None:
        if not (static_dir / "index.html").is_file():
            raise ValueError("desktop static directory has no index.html")
        application.mount("/", StaticFiles(directory=static_dir, html=True), name="desktop-ui")
```

Import `Path` and `StaticFiles`. Mounting last ensures `/api`, `/health`, and `/desktop/bootstrap` keep precedence.

- [ ] **Step 5: Run focused and existing health tests**

Run: `uv run pytest backend/tests/unit/desktop/test_server.py backend/tests/unit/test_health.py -v`

Expected: all selected tests pass.

- [ ] **Step 6: Commit loopback hosting**

```bash
git add backend/app/desktop/server.py backend/app/main.py backend/tests/unit/desktop/test_server.py
git commit -m "feat: serve desktop UI on random loopback port"
```

### Task 4: Private PostgreSQL process manager

**Files:**
- Create: `backend/app/desktop/postgres.py`
- Create: `backend/tests/unit/desktop/test_postgres.py`

- [ ] **Step 1: Write failing command-construction tests**

```python
from pathlib import Path

from backend.app.desktop.postgres import PostgresPaths, build_initdb_command, build_postgres_command


def test_initdb_rejects_network_auth_and_uses_local_role(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    command = build_initdb_command(paths)

    assert command == [
        str(paths.bin_dir / "initdb"),
        "--pgdata", str(paths.data_dir),
        "--username", "poly",
        "--encoding", "UTF8",
        "--auth-local", "trust",
        "--auth-host", "reject",
        "--no-instructions",
    ]


def test_postgres_has_no_tcp_listener_and_private_socket(tmp_path: Path) -> None:
    paths = PostgresPaths.for_test(tmp_path)
    command = build_postgres_command(paths)

    assert "listen_addresses=" in command
    assert f"unix_socket_directories={paths.socket_dir}" in command
    assert "unix_socket_permissions=0700" in command
    assert not any("0.0.0.0" in value or "::" in value for value in command)
```

- [ ] **Step 2: Run tests and confirm the manager is absent**

Run: `uv run pytest backend/tests/unit/desktop/test_postgres.py -v`

Expected: FAIL importing `backend.app.desktop.postgres`.

- [ ] **Step 3: Add immutable paths and exact PostgreSQL commands**

```python
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class PostgresPaths:
    bin_dir: Path
    data_dir: Path
    socket_dir: Path
    log_file: Path

    @classmethod
    def for_test(cls, root: Path) -> "PostgresPaths":
        return cls(root / "bin", root / "data", root / "socket", root / "postgres.log")

    @property
    def database_url(self) -> str:
        socket = quote(str(self.socket_dir), safe="")
        return f"postgresql+asyncpg://poly@/poly?host={socket}&port=5432"


def build_initdb_command(paths: PostgresPaths) -> list[str]:
    return [
        str(paths.bin_dir / "initdb"),
        "--pgdata", str(paths.data_dir),
        "--username", "poly",
        "--encoding", "UTF8",
        "--auth-local", "trust",
        "--auth-host", "reject",
        "--no-instructions",
    ]


def build_postgres_command(paths: PostgresPaths) -> list[str]:
    return [
        str(paths.bin_dir / "postgres"),
        "-D", str(paths.data_dir),
        "-c", "listen_addresses=",
        "-c", f"unix_socket_directories={paths.socket_dir}",
        "-c", "unix_socket_permissions=0700",
        "-c", "port=5432",
        "-c", "logging_collector=off",
    ]
```

- [ ] **Step 4: Add an injectable process runner and lifecycle tests**

Extend the test file with a fake runner recording `run`, `spawn`, and `terminate` calls. Test these ordered behaviors:

```python
async def test_first_start_initializes_starts_and_creates_database(tmp_path: Path) -> None:
    runner = FakeRunner()
    manager = PostgresManager(PostgresPaths.for_test(tmp_path), runner)

    await manager.start()

    assert [call.kind for call in runner.calls] == ["run", "spawn", "run", "run", "run"]
    assert runner.calls[0].argv[0].endswith("initdb")
    assert runner.calls[2].argv[0].endswith("pg_isready")
    assert runner.calls[3].argv[0].endswith("psql")
    assert runner.calls[4].argv[0].endswith("createdb")
```

The five calls are `initdb`, `postgres`, readiness polling, `psql -tAc "SELECT 1 FROM pg_database WHERE datname='poly'"`, and conditional `createdb`. On subsequent starts, skip `initdb`; always perform the query and skip `createdb` when it returns `1`. `stop()` sends SIGTERM, waits 10 seconds, then kills only the owned child handle.

- [ ] **Step 5: Enforce private directories**

Before process start, create `data_dir.parent` and `socket_dir` with mode `0700`; after creation call `chmod(0o700)` to correct an existing permissive directory. Reject symlinks for the data and socket directories using `Path.is_symlink()`.

- [ ] **Step 6: Run PostgreSQL manager tests**

Run: `uv run pytest backend/tests/unit/desktop/test_postgres.py -v`

Expected: command, first-start, repeat-start, timeout, symlink, permission, and owned-child shutdown tests all pass without launching real PostgreSQL.

- [ ] **Step 7: Commit the database manager**

```bash
git add backend/app/desktop/postgres.py backend/tests/unit/desktop/test_postgres.py
git commit -m "feat: manage private desktop postgres"
```

### Task 5: Runtime orchestration, migrations, and parent lease

**Files:**
- Create: `backend/app/desktop/runtime.py`
- Create: `backend/app/desktop/__main__.py`
- Create: `backend/tests/unit/desktop/test_runtime.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write a failing ordered-lifecycle test with fakes**

```python
import pytest

from backend.app.desktop.protocol import RuntimeState, StartCommand
from backend.app.desktop.runtime import DesktopRuntime


@pytest.mark.asyncio
async def test_runtime_migrates_before_starting_api_and_worker(tmp_path) -> None:
    trace: list[str] = []
    runtime = DesktopRuntime.for_test(trace)
    command = StartCommand(tmp_path / "data", tmp_path / "run", "x" * 43)

    ready = await runtime.start(command)

    assert trace == ["postgres.start", "migrate", "application.create", "server.start"]
    assert ready.state is RuntimeState.READY
    assert ready.fields["port"] == 49152
    assert str(ready.fields["bootstrap_path"]).startswith("/desktop/bootstrap/")
```

- [ ] **Step 2: Verify the orchestration test fails**

Run: `uv run pytest backend/tests/unit/desktop/test_runtime.py -v`

Expected: FAIL importing `DesktopRuntime`.

- [ ] **Step 3: Implement the dependency-injected orchestrator**

Define `PostgresLifecycle`, `MigrationRunner`, and `ServerLifecycle` protocols. `DesktopRuntime.start()` must emit, in order, `PREPARING_DATABASE`, `MIGRATING`, `STARTING_SERVICES`, and `READY`. It must set these runtime-only settings before building the application:

```python
runtime_settings = Settings(
    _env_file=None,
    opening_enabled=False,
    database_url=postgres.paths.database_url,
    credential_service_name="com.poly.desktop.integrations",
    local_setup_enabled=True,
)
```

Refactor `ApplicationContainer.runtime` and `create_app` to accept a `Settings` object, defaulting to the existing module settings for non-desktop callers. Add `manage_runtime_lifespan: bool | None = None` to `create_app`; its default remains `owns_container`, while desktop startup passes `True` with its explicitly created runtime container. This ensures the existing lifespan starts the worker, cancels it, and closes the container even though the desktop orchestrator supplied the container. Pass the same settings object to migrations rather than mutating environment variables or global settings.

Use Alembic programmatically:

```python
def upgrade_database(database_url: str, project_root: Path) -> None:
    config = AlembicConfig(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")
```

Adjust `migrations/env.py` to prefer an already supplied Alembic `sqlalchemy.url`; only fall back to application settings when the option is empty.

- [ ] **Step 4: Add safe shutdown and unclean-start tests**

```python
@pytest.mark.asyncio
async def test_shutdown_closes_opening_before_server_and_database(tmp_path) -> None:
    trace: list[str] = []
    runtime = DesktopRuntime.for_test(trace)
    await runtime.start(StartCommand(tmp_path / "data", tmp_path / "run", "x" * 43))

    await runtime.stop("application quit")

    assert trace[-4:] == [
        "opening.disable:application quit",
        "server.stop",
        "container.close",
        "postgres.stop",
    ]


@pytest.mark.asyncio
async def test_unclean_marker_keeps_opening_disabled_until_first_cycle(tmp_path) -> None:
    marker = tmp_path / "data" / "unclean_shutdown"
    marker.parent.mkdir(parents=True)
    marker.write_text("1", encoding="ascii")
    trace: list[str] = []
    runtime = DesktopRuntime.for_test(trace)

    await runtime.start(StartCommand(tmp_path / "data", tmp_path / "run", "x" * 43))

    assert "opening.disable:unclean desktop shutdown requires reconciliation" in trace
```

Create the marker atomically before PostgreSQL starts. Delete it only after server shutdown has completed the managed FastAPI lifespan, container close has been observed, and PostgreSQL stop succeeds. On startup with an existing marker, persist opening disabled before constructing the managed lifespan that starts the worker loop.

- [ ] **Step 5: Implement stdin lease and stdout events**

`backend/app/desktop/__main__.py` must read the first line as `StartCommand`, retain stdin, and send only `RuntimeEvent.to_json()` to stdout. A background task reads later lines with `asyncio.to_thread(sys.stdin.buffer.readline)`; EOF or `ShutdownCommand` calls `runtime.stop`. Exceptions become a sanitized `FAILED` event with stable error codes such as `migration_failed`, never raw credentials or command payloads.

Expose `python -m backend.app.desktop --self-test`, which verifies packaged frontend, PostgreSQL executables, migrations, and writable temporary directories without starting network services.

- [ ] **Step 6: Run runtime, migration, health, and live-loop tests**

Run: `uv run pytest backend/tests/unit/desktop backend/tests/unit/test_health.py backend/tests/unit/test_live_runtime_loop.py backend/tests/integration/db/test_initial_migration.py -v`

Expected: all selected tests pass; PostgreSQL integration test may use the repository's existing test database but no external market or AWS network call.

- [ ] **Step 7: Commit the complete local runtime**

```bash
git add backend/app/desktop backend/app/core/config.py backend/app/container.py backend/app/main.py migrations/env.py backend/tests/unit/desktop
git commit -m "feat: orchestrate packaged local runtime"
```

### Task 6: Reproducible PyInstaller onedir bundle

**Files:**
- Create: `packaging/macos/poly-runtime.spec`
- Create: `packaging/macos/build-runtime.sh`
- Create: `packaging/macos/verify-runtime.sh`
- Modify: `pyproject.toml`
- Modify: `.gitignore`

- [ ] **Step 1: Add build-only dependencies and lock them**

Add to the `dev` dependency group:

```toml
"pip-licenses>=5.0.0",
"pyinstaller>=6.22.0,<7",
```

Run: `uv lock`

Expected: `uv.lock` records PyInstaller, hooks, and license tooling without changing runtime dependency semantics.

- [ ] **Step 2: Add the concrete PyInstaller spec**

The spec must use `Analysis` with entry point `backend/app/desktop/__main__.py`, collect `keyring`, `sqlalchemy`, `asyncpg`, `uvicorn`, and `py_clob_client` submodules, and add these data trees:

```python
datas = [
    (str(ROOT / "migrations"), "migrations"),
    (str(ROOT / "alembic.ini"), "."),
    (str(ROOT / "frontend" / "dist"), "frontend/dist"),
    (str(ROOT / "packaging" / "macos" / "postgres" / TARGET), "postgres"),
]
```

Produce `COLLECT(..., name="poly-runtime")`, use `console=True` because Tauri pipes stdio and no separate terminal is created, set `target_arch` from `POLY_TARGET_ARCH`, and disable UPX.

- [ ] **Step 3: Add deterministic build and verification scripts**

`build-runtime.sh` must use `set -euo pipefail`, require `POLY_TARGET_ARCH` to be `arm64` or `x86_64`, run `npm ci` and `npm run build` in `frontend`, run `uv sync --frozen`, and then:

```bash
uv run pyinstaller --clean --noconfirm packaging/macos/poly-runtime.spec
```

`verify-runtime.sh` must run:

```bash
dist/poly-runtime/poly-runtime --self-test
file dist/poly-runtime/poly-runtime
find dist/poly-runtime -type f -perm -111 -print0 | xargs -0 file
```

It must fail if `file` reports the wrong CPU architecture or self-test reports a missing frontend, migration, PostgreSQL executable, or shared library.

- [ ] **Step 4: Ignore generated packaging outputs**

Append only these paths to `.gitignore`:

```gitignore
build/
packaging/macos/postgres/
src-tauri/resources/poly-runtime/
src-tauri/target/
```

- [ ] **Step 5: Run portable checks on Windows**

Run: `uv run pyinstaller --version`

Expected: a version in the supported 6.x range. Do not run the macOS spec on Windows and do not claim a macOS artifact exists.

- [ ] **Step 6: Commit packaging inputs**

```bash
git add pyproject.toml uv.lock .gitignore packaging/macos/poly-runtime.spec packaging/macos/build-runtime.sh packaging/macos/verify-runtime.sh
git commit -m "build: define packaged python runtime"
```

### Task 7: Tauri 2 shell and typed lifecycle state machine

**Files:**
- Create: `src-tauri/Cargo.toml`
- Create: `src-tauri/build.rs`
- Create: `src-tauri/tauri.conf.json`
- Create: `src-tauri/capabilities/main.json`
- Create: `src-tauri/src/main.rs`
- Create: `src-tauri/src/lib.rs`
- Create: `src-tauri/src/runtime/mod.rs`
- Create: `src-tauri/src/runtime/protocol.rs`
- Create: `src-tauri/src/runtime/state.rs`
- Modify: `frontend/package.json`

- [ ] **Step 1: Scaffold locked Tauri dependencies**

Use this dependency shape and let `cargo generate-lockfile` resolve current compatible patch releases:

```toml
[package]
name = "poly-desktop"
version = "0.1.0"
edition = "2024"

[lib]
name = "poly_desktop_lib"
crate-type = ["staticlib", "cdylib", "rlib"]

[build-dependencies]
tauri-build = { version = "2", features = [] }

[dependencies]
base64 = "0.22"
rand = "0.9"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tauri = { version = "2", features = [] }
tauri-plugin-single-instance = "2"
thiserror = "2"
tokio = { version = "1", features = ["io-util", "process", "sync", "time"] }
url = "2"
```

`build.rs` calls `tauri_build::build()`. `main.rs` calls `poly_desktop_lib::run()`.

- [ ] **Step 2: Write failing Rust state tests**

```rust
#[test]
fn ready_requires_a_port_and_bootstrap_path() {
    let raw = r#"{"version":1,"state":"ready","port":49152,"bootstrap_path":"/desktop/bootstrap/abc"}"#;
    let event: RuntimeEvent = serde_json::from_str(raw).unwrap();
    assert_eq!(event.state, RuntimeState::Ready);
    assert_eq!(event.port, Some(49152));
}

#[test]
fn deterministic_failure_does_not_auto_restart() {
    let state = SupervisorState::Running;
    let next = state.on_failure(FailureKind::Migration);
    assert_eq!(next, SupervisorState::Failed(FailureKind::Migration));
}
```

- [ ] **Step 3: Run tests and observe missing types**

Run: `cargo test --manifest-path src-tauri/Cargo.toml`

Expected: FAIL compiling unresolved `RuntimeEvent`, `RuntimeState`, and supervisor state types.

- [ ] **Step 4: Add strict Rust protocol and state types**

```rust
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeState {
    Initializing,
    PreparingDatabase,
    Migrating,
    StartingServices,
    Ready,
    ShuttingDown,
    Stopped,
    Failed,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct RuntimeEvent {
    pub version: u8,
    pub state: RuntimeState,
    #[serde(default)]
    pub port: Option<u16>,
    #[serde(default)]
    pub bootstrap_path: Option<String>,
    #[serde(default)]
    pub error_code: Option<String>,
}
```

Reject protocol versions other than 1 and reject `Ready` without a port, a loopback-safe bootstrap path beginning `/desktop/bootstrap/`, or with unknown fields. Model retryable failures separately from migration, resource, permission, and protocol failures.

- [ ] **Step 5: Configure the native window and least privilege**

Set identifier `com.poly.desktop`, product name `Poly`, minimum system version `12.0`, `frontendDist` to `../frontend/dist`, and `devUrl` to `http://127.0.0.1:5173`. The only capability is:

```json
{
  "$schema": "../gen/schemas/desktop-schema.json",
  "identifier": "main",
  "windows": ["main"],
  "permissions": ["core:default"]
}
```

Do not expose shell, filesystem, process, opener, or arbitrary HTTP plugin permissions to React. Rust owns sidecar and navigation operations.

- [ ] **Step 6: Run Cargo tests and frontend build**

Run: `cargo test --manifest-path src-tauri/Cargo.toml`

Expected: all Rust state/protocol tests pass.

Run: `npm --prefix frontend run build`

Expected: Vite production build succeeds.

- [ ] **Step 7: Commit the shell scaffold**

```bash
git add src-tauri frontend/package.json frontend/package-lock.json
git commit -m "feat: scaffold tauri desktop shell"
```

### Task 8: Rust runtime supervisor and bounded restart

**Files:**
- Create: `src-tauri/src/runtime/process.rs`
- Create: `src-tauri/src/runtime/supervisor.rs`
- Modify: `src-tauri/src/runtime/mod.rs`
- Modify: `src-tauri/src/lib.rs`

- [ ] **Step 1: Write failing retry-policy tests**

```rust
#[test]
fn retryable_crashes_back_off_and_stop_after_limit() {
    let policy = RestartPolicy::new(3, Duration::from_secs(1), Duration::from_secs(8));
    assert_eq!(policy.delay_for(1), Some(Duration::from_secs(1)));
    assert_eq!(policy.delay_for(2), Some(Duration::from_secs(2)));
    assert_eq!(policy.delay_for(3), Some(Duration::from_secs(4)));
    assert_eq!(policy.delay_for(4), None);
}

#[tokio::test]
async fn supervisor_sends_start_over_stdin_not_arguments() {
    let launcher = FakeLauncher::default();
    let mut supervisor = RuntimeSupervisor::new(launcher.clone(), RestartPolicy::test());

    supervisor.start(PathBuf::from("/data"), PathBuf::from("/run")).await.unwrap();

    assert!(launcher.arguments().is_empty());
    let start: serde_json::Value = serde_json::from_slice(&launcher.stdin_bytes()).unwrap();
    assert_eq!(start["command"], "start");
    assert!(start["launch_token"].as_str().unwrap().len() >= 43);
}
```

- [ ] **Step 2: Verify tests fail before process adapter exists**

Run: `cargo test --manifest-path src-tauri/Cargo.toml runtime::supervisor`

Expected: FAIL compiling missing supervisor types.

- [ ] **Step 3: Implement restart policy and injectable launcher**

Create a `RuntimeLauncher` trait returning a `RuntimeChild` trait with stdin write, stdout event stream, exit wait, graceful shutdown, and owned-child kill operations. Production code resolves:

```text
Contents/Resources/poly-runtime/poly-runtime
```

from Tauri's resource directory and refuses a symlink or a path outside that resource directory. Spawn with empty argument list, piped stdin/stdout/stderr, `kill_on_drop(true)`, and `env_clear()` followed by a minimal allowlist: `PATH=/usr/bin:/bin`, `LANG`, `LC_ALL`, `TMPDIR`, and Keychain-related standard macOS context only. Do not copy application `.env` variables.

Generate the 256-bit launch token using `rand::rngs::OsRng`, base64url without padding. Write exactly one start JSON line to stdin and retain the handle as the parent lease.

- [ ] **Step 4: Parse output and redact diagnostics**

Read stdout by newline, cap each line at 64 KiB, reject unknown protocol fields, and publish typed state to the Tauri layer. Read stderr separately into 1 MiB rotating files under `Application Support/Poly/logs`; before writing, replace case-insensitive `authorization`, `cookie`, `private_key`, `secret`, `signature`, and `token` values with `[REDACTED]`.

- [ ] **Step 5: Implement shutdown and recovery**

On graceful quit, write a versioned shutdown JSON line, wait 15 seconds, then terminate the exact owned child handle and its owned process group. On child failure:

- Migration, permissions, missing resources, and protocol mismatch become terminal UI states.
- Unexpected exits restart at 1, 2, and 4 seconds plus bounded jitter.
- A fourth crash within 60 seconds becomes terminal until the operator presses retry.
- A stable 5-minute run resets the attempt counter.

Never kill a process by reading an unverified PID file.

- [ ] **Step 6: Run supervisor tests**

Run: `cargo test --manifest-path src-tauri/Cargo.toml runtime::`

Expected: protocol, no-argument secret transport, retry, terminal-failure, line-limit, redaction, EOF, graceful shutdown, and owned-child timeout tests pass.

- [ ] **Step 7: Commit supervision**

```bash
git add src-tauri/src/runtime src-tauri/src/lib.rs
git commit -m "feat: supervise local desktop runtime"
```

### Task 9: Native startup, reconnect, and error presentation

**Files:**
- Create: `frontend/src/desktop/DesktopBootScreen.tsx`
- Create: `frontend/src/desktop/DesktopBootScreen.test.tsx`
- Create: `frontend/src/desktop/desktop.css`
- Modify: `frontend/src/main.tsx`
- Create: `src-tauri/src/webview.rs`
- Modify: `src-tauri/src/lib.rs`

- [ ] **Step 1: Write failing status UI tests**

```tsx
import { render, screen } from '@testing-library/react'
import { describe, expect, test } from 'vitest'

import { DesktopBootScreen } from './DesktopBootScreen'

describe('DesktopBootScreen', () => {
  test.each([
    ['initializing', '正在准备 Poly'],
    ['preparing_database', '正在准备本地数据库'],
    ['migrating', '正在升级本地数据'],
    ['starting_services', '正在启动服务'],
    ['restarting', '服务中断，正在重启'],
    ['keychain_denied', '无法访问系统钥匙串'],
    ['migration_failed', '本地数据升级失败'],
    ['runtime_unavailable', '本地服务无法启动'],
    ['shutting_down', '正在安全退出'],
  ])('renders %s', (state, copy) => {
    render(<DesktopBootScreen state={state} />)
    expect(screen.getByText(copy)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Verify the component does not exist**

Run: `npm --prefix frontend test -- DesktopBootScreen.test.tsx`

Expected: FAIL resolving `./DesktopBootScreen`.

- [ ] **Step 3: Implement the isolated boot screen**

Define a `DesktopUiState` union matching Rust's serializable state. Render one heading, one explanatory sentence, a progress indicator for transient states, and a retry button only for retryable/terminal failure states. Render “在 Finder 中显示诊断日志” only when Rust supplies `canRevealLogs=true`; wire button events through a narrow `window.__POLY_DESKTOP__` testable adapter rather than importing shell/filesystem plugins.

In `main.tsx`, render the boot screen only when the bundled URL contains `?desktop-state=`. Normal HTTP-served React continues to render `<App />`, so existing business UI and dirty user changes in `App.tsx`/`App.css` are not rewritten.

- [ ] **Step 4: Add Rust navigation helpers**

`webview.rs` exposes only:

```rust
pub fn status_url(state: &str) -> Url {
    Url::parse(&format!("tauri://localhost/?desktop-state={state}")).expect("valid bundled URL")
}

pub fn ready_url(port: u16, bootstrap_path: &str) -> Result<Url, NavigationError> {
    if !bootstrap_path.starts_with("/desktop/bootstrap/") {
        return Err(NavigationError::UnsafeBootstrapPath);
    }
    Url::parse(&format!("http://127.0.0.1:{port}{bootstrap_path}"))
        .map_err(NavigationError::InvalidUrl)
}
```

The Rust controller navigates to bundled status URLs for startup, retry, failure, and shutdown. It navigates to the loopback URL only after validating a `READY` event. A sidecar disconnect immediately returns the WebView to `restarting`, so the dead page cannot keep accepting operator input.

- [ ] **Step 5: Run frontend and Rust tests**

Run: `npm --prefix frontend test -- DesktopBootScreen.test.tsx`

Expected: all status cases pass.

Run: `cargo test --manifest-path src-tauri/Cargo.toml webview`

Expected: loopback URL succeeds; non-loopback hosts, invalid ports, and unsafe bootstrap paths fail.

- [ ] **Step 6: Commit status and navigation UX**

```bash
git add frontend/src/desktop frontend/src/main.tsx src-tauri/src/webview.rs src-tauri/src/lib.rs
git commit -m "feat: show desktop runtime lifecycle"
```

### Task 10: Single instance and application shutdown lifecycle

**Files:**
- Create: `src-tauri/src/app_lifecycle.rs`
- Modify: `src-tauri/src/lib.rs`
- Create: `src-tauri/tests/lifecycle.rs`

- [ ] **Step 1: Write failing lifecycle tests against a fake supervisor**

```rust
#[tokio::test]
async fn close_request_is_prevented_until_runtime_stops() {
    let supervisor = FakeSupervisor::running();
    let decision = handle_close_request(&supervisor).await;

    assert_eq!(decision, CloseDecision::PreventAndShutdown);
    assert_eq!(supervisor.messages(), vec!["application quit"]);
}

#[tokio::test]
async fn second_instance_focuses_existing_window_without_second_runtime() {
    let app = FakeApplication::default();
    handle_second_instance(&app);

    assert_eq!(app.show_count(), 1);
    assert_eq!(app.focus_count(), 1);
    assert_eq!(app.runtime_start_count(), 0);
}
```

- [ ] **Step 2: Verify lifecycle types are missing**

Run: `cargo test --manifest-path src-tauri/Cargo.toml --test lifecycle`

Expected: FAIL compiling missing lifecycle functions.

- [ ] **Step 3: Implement single instance and two-phase close**

Initialize `tauri-plugin-single-instance` before app setup. A second launch shows and focuses the existing main window only.

For close events:

1. Prevent the first close event.
2. Navigate to `shutting_down`.
3. Await supervisor shutdown on Tauri's async runtime.
4. Set an atomic `shutdown_complete` flag.
5. Call `app.exit(0)`; later close events are not prevented.

Track the owned runtime child in application state. During Tauri teardown, if graceful shutdown did not complete, call the supervisor's owned-child cleanup once.

- [ ] **Step 4: Run lifecycle and full Rust tests**

Run: `cargo test --manifest-path src-tauri/Cargo.toml`

Expected: all Rust unit and integration tests pass.

- [ ] **Step 5: Commit lifecycle integration**

```bash
git add src-tauri/src/app_lifecycle.rs src-tauri/src/lib.rs src-tauri/tests/lifecycle.rs
git commit -m "feat: stop local services when Poly exits"
```

### Task 11: PostgreSQL distribution, entitlements, and third-party notices

**Files:**
- Create: `packaging/macos/fetch-postgres.sh`
- Create: `packaging/macos/postgres-SHA256SUMS`
- Create: `packaging/macos/generate-notices.sh`
- Create: `packaging/macos/entitlements.plist`
- Create: `packaging/macos/verify-bundle.sh`
- Create: `THIRD_PARTY_NOTICES.md`
- Modify: `src-tauri/tauri.conf.json`

- [ ] **Step 1: Pin PostgreSQL source and verify its digest**

Use PostgreSQL 16.15 source from `https://ftp.postgresql.org/pub/source/v16.15/postgresql-16.15.tar.bz2`. Commit this exact official checksum in `postgres-SHA256SUMS` and make `fetch-postgres.sh` run `shasum -a 256 -c` before extraction:

```text
c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed  postgresql-16.15.tar.bz2
```

The script builds on the native runner architecture with:

```bash
./configure --prefix="$PWD/install" --without-readline --without-zlib --disable-nls
make -j"$(sysctl -n hw.logicalcpu)" world-bin
make install-world-bin
```

Copy only required client/server programs, `libpq`, timezone/share files, and extension files into `packaging/macos/postgres/$POLY_TARGET_TRIPLE`. Run `otool -L` over every executable and dylib; fail if any dependency points to a Homebrew, MacPorts, runner workspace, or user path.

- [ ] **Step 2: Generate complete notices**

`generate-notices.sh` combines:

- `cargo about generate` or `cargo license` output for Rust dependencies.
- `pip-licenses --format=markdown` for the frozen Python environment.
- npm production dependency licenses from the lockfile.
- CPython, PyInstaller bootloader, PostgreSQL, Tauri/WebKit usage notices, and copied native library licenses.

Fail the build if a packaged file has no corresponding license inventory entry. Commit the generated `THIRD_PARTY_NOTICES.md` and include it in Tauri resources.

- [ ] **Step 3: Configure packaged resources and hardened runtime**

Copy PyInstaller's complete onedir tree to `src-tauri/resources/poly-runtime/`; do not use Tauri `externalBin` for only the launcher because PyInstaller's `_internal` directory must remain adjacent. Configure that entire tree plus `THIRD_PARTY_NOTICES.md` as bundle resources.

Use an empty entitlements file; the application does not need JIT, unsigned executable memory, App Sandbox, or network-server entitlements:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
</dict>
</plist>
```

Do not add App Sandbox, unrestricted file access, network server, unsigned executable memory, or library validation exceptions unless a failing signed Mac test proves a narrowly scoped entitlement is required and the design receives a security amendment.

- [ ] **Step 4: Add bundle verification**

`verify-bundle.sh` locates the built `.app` and performs:

```bash
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
spctl --assess --type execute --verbose=4 "$APP_PATH"
xcrun stapler validate "$APP_PATH"
```

It then launches the App, waits for its loopback service, quits it through Apple Events, and fails if `pgrep -f "$APP_PATH/Contents/Resources/poly-runtime"` or `lsof` finds a remaining Poly runtime, PostgreSQL process, or listener. It must use paths resolved inside the `.app`, never broad process-name killing.

- [ ] **Step 5: Run script syntax and notice checks**

Run on macOS: `bash -n packaging/macos/*.sh`

Expected: all scripts parse.

Run: `packaging/macos/generate-notices.sh --check`

Expected: committed notices match the current lockfiles.

- [ ] **Step 6: Commit release resources**

```bash
git add packaging/macos src-tauri/tauri.conf.json THIRD_PARTY_NOTICES.md
git commit -m "build: assemble licensed macOS resources"
```

### Task 12: macOS CI, installation smoke test, and operator documentation

**Files:**
- Create: `.github/workflows/macos-desktop.yml`
- Create: `docs/runbooks/macos-desktop-build.md`
- Modify: `README.md`

- [ ] **Step 1: Add architecture-native CI matrix**

Use this matrix so PyInstaller and PostgreSQL are built on the target CPU rather than cross-compiled:

```yaml
strategy:
  fail-fast: false
  matrix:
    include:
      - runner: macos-15
        rust_target: aarch64-apple-darwin
        py_arch: arm64
        artifact: Poly-macos-arm64
      - runner: macos-15-intel
        rust_target: x86_64-apple-darwin
        py_arch: x86_64
        artifact: Poly-macos-x86_64
runs-on: ${{ matrix.runner }}
```

Pin action major versions and grant `contents: read`. Pull requests run unit tests, build PostgreSQL/runtime/App, ad-hoc sign, and upload non-release diagnostics. Signed/notarized DMGs run only on an explicitly tagged release after protected secrets are present.

- [ ] **Step 2: Configure signed release without exposing secrets**

Use Tauri's documented CI variables: `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `KEYCHAIN_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, and `APPLE_TEAM_ID`. Never echo them. Import the temporary keychain only in the signed job and delete that temporary keychain in an `always()` cleanup step.

Run `npm ci`, `uv sync --frozen`, Python tests with all market transports mocked, frontend tests/build, Cargo tests, PostgreSQL build, PyInstaller build, `cargo tauri build --target`, notarization, staple, and `verify-bundle.sh` in that order.

- [ ] **Step 3: Add a clean-user smoke test**

Create a temporary macOS user or isolated temporary home on the runner. Assert `python`, `node`, `postgres`, `aws`, `docker`, and `brew` are not consulted by tracing child executable paths. Install the DMG to a temporary Applications directory, launch twice to verify single instance, save a synthetic Keychain test item under the stable service name, relaunch, and verify it can be read. Never use real market credentials.

- [ ] **Step 4: Document exact Mac build and installation steps**

The runbook must document:

- Required build-time Apple Developer membership and CI secrets.
- `workflow_dispatch` for unsigned validation and release tags for signed builds.
- Where to download each architecture-specific DMG.
- Drag-to-Applications install and first Keychain prompt.
- Persistent data location and safe stopped-state backup procedure.
- Log location and recovery steps for migration, Keychain, or runtime errors.
- Explicit statement that the operator installs no other runtime environment.
- Explicit statement that Windows verification does not prove a macOS bundle works.

Update README with a short “macOS desktop” section linking to the runbook and preserving current server-style development instructions.

- [ ] **Step 5: Run the full portable verification suite on the current machine**

Run:

```bash
uv run pytest backend/tests/unit backend/tests/security -q
npm --prefix frontend test
npm --prefix frontend run build
cargo test --manifest-path src-tauri/Cargo.toml
```

Expected: all portable backend, frontend, and Rust tests pass. Record integration tests skipped because they require the repository's test PostgreSQL service.

- [ ] **Step 6: Run macOS release verification in CI**

Run both matrix jobs through GitHub Actions without deployment to AWS or any cloud runtime. Expected for each architecture: tests pass, App and DMG build, nested signing passes, notarization is accepted and stapled, Gatekeeper assessment passes, and clean-user install/quit smoke leaves no child process or listener.

If Intel runner cost is unacceptable, keep the Intel job manually dispatchable and publish only Apple Silicon until the same checks have passed on Intel. Do not label an untested Intel file as supported.

- [ ] **Step 7: Commit CI and operator guidance**

```bash
git add .github/workflows/macos-desktop.yml docs/runbooks/macos-desktop-build.md README.md
git commit -m "ci: build and verify macOS desktop packages"
```

## Final verification checkpoint

- [ ] Run `git status --short` and confirm only intentional implementation files are changed; preserve the pre-existing frontend edits and untracked user files.
- [ ] Run `uv run ruff check backend` and `uv run mypy backend`.
- [ ] Run `uv run pytest backend/tests/unit backend/tests/security -q`.
- [ ] Run the available PostgreSQL-backed integration suite against the repository's isolated test database, never a production database.
- [ ] Run `npm --prefix frontend run lint`, `npm --prefix frontend test`, and `npm --prefix frontend run build`.
- [ ] Run `cargo fmt --manifest-path src-tauri/Cargo.toml -- --check`, `cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings`, and `cargo test --manifest-path src-tauri/Cargo.toml`.
- [ ] Review `git diff` for secrets, `.env` contents, fixed credentials, AWS configuration, wildcard listeners, shell permissions, and long-lived access tokens.
- [ ] On macOS CI, verify both architecture jobs or explicitly mark Intel unsupported.
- [ ] On a real Apple Silicon Mac, install the stapled DMG into Applications and verify first launch, restart, Keychain access, normal quit, crash recovery, and zero residual processes/ports.

The project is not complete until the real Mac checks have evidence. A successful Windows test run is only portable-test evidence and must not be reported as a generated or validated `.dmg`.

## Implementation references

- [Tauri 2 existing-frontend setup](https://v2.tauri.app/start/create-project/)
- [Tauri 2 external binaries and target triples](https://v2.tauri.app/develop/sidecar/)
- [Tauri 2 capability boundaries](https://v2.tauri.app/security/capabilities/)
- [Tauri macOS signing and notarization](https://v2.tauri.app/distribute/sign/macos/)
- [PyInstaller usage and macOS architecture options](https://pyinstaller.org/en/stable/usage.html)
- [PostgreSQL 16 connection and Unix socket settings](https://www.postgresql.org/docs/16/runtime-config-connection.html)
- [PostgreSQL trust authentication boundary](https://www.postgresql.org/docs/16/auth-trust.html)
- [GitHub-hosted macOS runner architectures](https://github.com/actions/runner-images)
