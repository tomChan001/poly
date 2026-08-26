import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.config import Settings, TradingMode
from backend.app.main import create_app


def test_missing_environment_defaults_to_read_only_and_closed() -> None:
    configured = Settings(_env_file=None)

    assert configured.trading_mode is TradingMode.READ_ONLY
    assert configured.opening_enabled is False


@pytest.mark.asyncio
async def test_health_reports_safe_default_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app import main as main_module

    monkeypatch.setattr(main_module.settings, "trading_mode", TradingMode.READ_ONLY)
    container = ApplicationContainer()
    container.system_control.disable_opening("configured default")
    transport = httpx.ASGITransport(app=create_app(container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "trading_mode": "read_only",
        "opening_enabled": False,
        "reason": "configured default",
    }
