from decimal import Decimal

import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.security import Principal, Role, get_current_principal
from backend.app.main import create_app
from backend.app.services.opportunities import OpportunityRecord


@pytest.mark.asyncio
async def test_opportunities_are_sorted_by_roi_and_explain_rejections() -> None:
    container = ApplicationContainer()
    container.opportunities.replace(
        [
            OpportunityRecord.example("low", Decimal("0.04"), ()),
            OpportunityRecord.example("high", Decimal("0.08"), ()),
            OpportunityRecord.example("rejected", Decimal(0), ("STALE_BOOK",)),
        ]
    )
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "viewer",
        frozenset({Role.VIEWER}),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/opportunities")

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == ["high", "low", "rejected"]
    assert body[-1]["rejection_reasons"] == ["STALE_BOOK"]


@pytest.mark.asyncio
async def test_business_api_fails_closed_without_oidc_verifier() -> None:
    transport = httpx.ASGITransport(app=create_app(ApplicationContainer()))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/opportunities")

    assert response.status_code == 503
