import httpx
import pytest
from fastapi import FastAPI

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
async def test_read_only_deployment_cannot_enable_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.api.routes import system_control as route_module

    container = ApplicationContainer()
    container.system_control.opening_enabled = False
    monkeypatch.setattr(route_module.settings, "trading_mode", TradingMode.READ_ONLY)

    transport = httpx.ASGITransport(app=app_for(container, Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/system-control/opening",
            json={"enabled": True, "reason": "premature enable"},
        )

    assert response.status_code == 409
    assert "limited_auto" in response.json()["detail"]
    assert container.system_control.opening_enabled is False


@pytest.mark.asyncio
async def test_missing_automation_evidence_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.api.routes import system_control as route_module

    container = ApplicationContainer()
    container.system_control.opening_enabled = False
    monkeypatch.setattr(route_module.settings, "trading_mode", TradingMode.LIMITED_AUTO)

    transport = httpx.ASGITransport(app=app_for(container, Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/system-control/opening",
            json={"enabled": True, "reason": "no evidence"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "automation evidence is missing"
    assert container.system_control.opening_enabled is False
