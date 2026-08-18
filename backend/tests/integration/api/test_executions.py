import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.security import Principal, Role, get_current_principal
from backend.app.main import create_app


@pytest.mark.asyncio
async def test_execution_api_is_read_only_and_has_no_per_trade_approval() -> None:
    container = ApplicationContainer()
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "viewer",
        frozenset({Role.VIEWER}),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        list_response = await client.get("/api/executions")
        approval_response = await client.post("/api/executions/execution-1/approve")

    assert list_response.json() == []
    assert approval_response.status_code == 404
