import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.security import Principal, Role, get_current_principal
from backend.app.main import create_app
from backend.app.services.integration_config import (
    IntegrationEnvironment,
    IntegrationProvider,
)


def app_for(container: ApplicationContainer):
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "local-operator",
        frozenset({Role.OPERATOR}),
    )
    return app


@pytest.mark.asyncio
async def test_runtime_status_reports_exact_missing_configuration_without_secrets() -> None:
    container = ApplicationContainer()
    container.system_control.set_opening(True, "test fixture")
    await container.integration_configs.update(
        IntegrationProvider.ODDPOOL,
        enabled=True,
        environment=IntegrationEnvironment.PRODUCTION,
        base_url="https://oddpool.test",
        configuration={},
        secrets={"api_token": "must-not-leak"},
        actor="local-operator",
    )
    transport = httpx.ASGITransport(app=app_for(container))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/runtime")

    assert response.status_code == 200
    assert response.json() == {
        "ready": False,
        "running": False,
        "opening_enabled": True,
        "missing_providers": ["kalshi", "polymarket"],
        "last_cycle_at": None,
        "last_error": None,
        "executions_started": 0,
    }
    assert "must-not-leak" not in response.text
