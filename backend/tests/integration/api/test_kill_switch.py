import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from backend.app.container import ApplicationContainer
from backend.app.core.config import TradingMode
from backend.app.core.security import Principal, Role, get_current_principal
from backend.app.main import create_app
from backend.app.services.system_control import OpeningControlState, SystemControl


class RecordingControlStore:
    def __init__(self) -> None:
        self.saved: list[tuple[bool, str, str]] = []
        self.state: OpeningControlState | None = None

    async def load_opening(self) -> OpeningControlState | None:
        return self.state

    async def save_opening(
        self,
        *,
        enabled: bool,
        reason: str,
        changed_by: str,
    ) -> OpeningControlState:
        self.saved.append((enabled, reason, changed_by))
        self.state = OpeningControlState(
            opening_enabled=enabled,
            reason=reason,
            version=len(self.saved),
            changed_by=changed_by,
        )
        return self.state


class UnavailableControlStore:
    async def load_opening(self) -> OpeningControlState | None:
        return None

    async def save_opening(
        self,
        *,
        enabled: bool,
        reason: str,
        changed_by: str,
    ) -> OpeningControlState:
        raise SQLAlchemyError("database connection unavailable")


class RetryingControlStore:
    def __init__(self) -> None:
        self.calls = 0

    async def load_opening(self) -> OpeningControlState | None:
        return None

    async def save_opening(
        self,
        *,
        enabled: bool,
        reason: str,
        changed_by: str,
    ) -> OpeningControlState:
        self.calls += 1
        if self.calls == 1:
            raise SQLAlchemyError("database connection unavailable")
        return OpeningControlState(
            opening_enabled=False,
            reason="durably closed",
            version=9,
            changed_by="durable-store",
        )


def app_for(container: ApplicationContainer, role: Role | None) -> FastAPI:
    app = create_app(container)
    if role is not None:
        app.dependency_overrides[get_current_principal] = lambda: Principal("user-1", frozenset({role}))
    return app


@pytest.mark.asyncio
async def test_system_control_denies_remote_access() -> None:
    transport = httpx.ASGITransport(app=app_for(ApplicationContainer(), None))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/system-control/opening",
            json={"enabled": False, "reason": "incident"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "local access only"


@pytest.mark.asyncio
async def test_only_operator_can_change_kill_switch() -> None:
    container = ApplicationContainer()
    store = RecordingControlStore()
    container.system_control = SystemControl(
        opening_enabled=True,
        reason="configured open",
        store=store,
    )

    viewer_transport = httpx.ASGITransport(app=app_for(container, Role.VIEWER))
    operator_transport = httpx.ASGITransport(app=app_for(container, Role.OPERATOR))
    async with httpx.AsyncClient(transport=viewer_transport, base_url="http://test") as client:
        viewer_response = await client.put(
            "/api/system-control/opening",
            json={"enabled": False, "reason": "viewer attempt"},
        )
    async with httpx.AsyncClient(transport=operator_transport, base_url="http://test") as client:
        operator_response = await client.put(
            "/api/system-control/opening",
            json={"enabled": False, "reason": "reconciliation mismatch"},
        )

    assert viewer_response.status_code == 403
    assert operator_response.status_code == 200
    assert operator_response.json() == {
        "opening_enabled": False,
        "reason": "reconciliation mismatch",
        "version": 1,
    }
    assert store.saved == [(False, "reconciliation mismatch", "user-1")]


@pytest.mark.asyncio
async def test_kill_switch_reason_cannot_be_blank() -> None:
    transport = httpx.ASGITransport(app=app_for(ApplicationContainer(), Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/system-control/opening",
            json={"enabled": False, "reason": "  "},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_operator_can_enable_opening_without_mode_or_automation_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.core import config

    container = ApplicationContainer()
    store = RecordingControlStore()
    container.system_control = SystemControl(store=store)
    container.automation_evidence = None
    monkeypatch.setattr(config.settings, "trading_mode", TradingMode.READ_ONLY)

    transport = httpx.ASGITransport(app=app_for(container, Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/system-control/opening",
            json={"enabled": True, "reason": "operator authorized"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "opening_enabled": True,
        "reason": "operator authorized",
        "version": 1,
    }
    assert store.saved == [(True, "operator authorized", "user-1")]


@pytest.mark.asyncio
async def test_disabling_opening_fails_closed_when_persistence_is_unavailable() -> None:
    container = ApplicationContainer()
    container.system_control = SystemControl(
        opening_enabled=True,
        reason="active opening",
        version=4,
        store=UnavailableControlStore(),
    )
    transport = httpx.ASGITransport(
        app=app_for(container, Role.OPERATOR),
        raise_app_exceptions=False,
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/system-control/opening",
            json={"enabled": False, "reason": "manual shutdown"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "opening control persistence unavailable"}
    assert container.system_control.opening_enabled is False
    assert container.system_control.reason == "manual shutdown"
    assert container.system_control.version == 5
    assert container.system_control.changed_by == "user-1"


@pytest.mark.asyncio
async def test_enabling_opening_keeps_it_closed_when_persistence_is_unavailable() -> None:
    container = ApplicationContainer()
    container.system_control = SystemControl(
        opening_enabled=False,
        reason="safe default",
        version=4,
        store=UnavailableControlStore(),
    )
    transport = httpx.ASGITransport(
        app=app_for(container, Role.OPERATOR),
        raise_app_exceptions=False,
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/system-control/opening",
            json={"enabled": True, "reason": "operator authorized"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "opening control persistence unavailable"}
    assert container.system_control.opening_enabled is False
    assert container.system_control.reason == "safe default"
    assert container.system_control.version == 4


@pytest.mark.asyncio
async def test_successful_retry_uses_the_durable_closing_state() -> None:
    container = ApplicationContainer()
    store = RetryingControlStore()
    container.system_control = SystemControl(
        opening_enabled=True,
        reason="active opening",
        version=4,
        store=store,
    )
    transport = httpx.ASGITransport(
        app=app_for(container, Role.OPERATOR),
        raise_app_exceptions=False,
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        failed_response = await client.put(
            "/api/system-control/opening",
            json={"enabled": False, "reason": "manual shutdown"},
        )
        retry_response = await client.put(
            "/api/system-control/opening",
            json={"enabled": False, "reason": "retry shutdown"},
        )

    assert failed_response.status_code == 503
    assert retry_response.status_code == 200
    assert retry_response.json() == {
        "opening_enabled": False,
        "reason": "durably closed",
        "version": 9,
    }
    assert container.system_control.reason == "durably closed"
    assert container.system_control.version == 9
    assert container.system_control.changed_by == "durable-store"
