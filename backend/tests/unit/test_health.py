import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.config import Settings
from backend.app.main import create_app


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
