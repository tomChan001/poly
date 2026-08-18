import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.main import create_app


@pytest.mark.asyncio
async def test_settings_update_creates_new_version() -> None:
    container = ApplicationContainer()
    original_version = str(container.risk_policies.current.version)
    app = create_app(container)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/settings/risk",
            json={
                "minimum_roi": "0.05",
                "maximum_settlement_days": 20,
                "maximum_book_age_seconds": "1.5",
                "per_trade_limit": "10",
                "per_event_limit": "25",
                "portfolio_limit": "100",
                "explicit_cost": "0",
                "risk_buffer": "0.25",
                "maximum_unhedged_seconds": "2",
                "maximum_unhedged_loss": "2"
            },
        )

    assert response.status_code == 200
    assert response.json()["minimum_roi"] == "0.05"
    assert response.json()["version"] != original_version
