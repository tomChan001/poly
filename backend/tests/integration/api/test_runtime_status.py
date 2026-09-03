import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.security import Principal, Role, get_current_principal
from backend.app.main import create_app
from backend.app.services.integration_config import (
    ODDPOOL_BASE_URL,
    IntegrationEnvironment,
    IntegrationProvider,
    RuntimeReadiness,
)
from backend.app.services.runtime_status import RuntimeStatusService


class FakeReadyIntegrations:
    async def readiness(self) -> RuntimeReadiness:
        return RuntimeReadiness(ready=True, missing=())


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
        base_url=ODDPOOL_BASE_URL,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("opening_enabled", [False, True])
async def test_runtime_status_separates_ready_running_from_opening_permission(
    opening_enabled: bool,
) -> None:
    container = ApplicationContainer()
    container.system_control.set_opening(opening_enabled, "test fixture")
    container.runtime_status = RuntimeStatusService(
        FakeReadyIntegrations(),  # type: ignore[arg-type]
        container.system_control,
    )
    container.runtime_status.running = True
    transport = httpx.ASGITransport(app=app_for(container))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/runtime")

    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "running": True,
        "opening_enabled": opening_enabled,
        "missing_providers": [],
        "last_cycle_at": None,
        "last_error": None,
        "executions_started": 0,
    }
    assert "test fixture" not in response.text
