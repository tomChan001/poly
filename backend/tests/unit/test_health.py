import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from backend.app.container import ApplicationContainer
from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.services.system_control import OpeningControlState, SystemControl


class DurableHealthStore:
    async def load_opening(self) -> OpeningControlState | None:
        return OpeningControlState(False, "durably disabled", version=7)

    async def save_opening(self, **_: object) -> OpeningControlState:
        raise AssertionError("health must not write durable state")


class UnavailableHealthStore(DurableHealthStore):
    async def load_opening(self) -> OpeningControlState | None:
        raise SQLAlchemyError("database unavailable")


def test_missing_environment_defaults_to_closed() -> None:
    configured = Settings(_env_file=None)

    assert configured.opening_enabled is False


@pytest.mark.asyncio
async def test_health_reports_safe_default_without_legacy_mode() -> None:
    container = ApplicationContainer()
    container.system_control.disable_opening("configured default")
    transport = httpx.ASGITransport(app=create_app(container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "opening_enabled": False,
        "reason": "configured default",
    }
    assert "trading_mode" not in response.json()


@pytest.mark.asyncio
async def test_health_refreshes_durable_opening_state_before_responding() -> None:
    container = ApplicationContainer()
    container.system_control = SystemControl(
        opening_enabled=True,
        reason="stale local state",
        store=DurableHealthStore(),
    )
    transport = httpx.ASGITransport(app=create_app(container))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["opening_enabled"] is False
    assert response.json()["reason"] == "durably disabled"


@pytest.mark.asyncio
async def test_health_refuses_stale_permission_when_durable_refresh_fails() -> None:
    container = ApplicationContainer()
    container.system_control = SystemControl(
        opening_enabled=True,
        reason="stale local state",
        store=UnavailableHealthStore(),
    )
    transport = httpx.ASGITransport(app=create_app(container), raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "opening control persistence unavailable"}
