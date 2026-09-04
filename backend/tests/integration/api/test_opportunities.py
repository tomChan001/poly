from dataclasses import replace
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
    rejected = replace(
        OpportunityRecord.example("rejected", Decimal(0), ("STALE_BOOK",)),
        quantity=None,
        kalshi_vwap=None,
        polymarket_vwap=None,
        total_fees=None,
        deployed_capital=None,
        payout=None,
        profit_floor=None,
        conservative_roi=None,
        book_age_ms=None,
    )
    container.opportunities.replace(
        [
            OpportunityRecord.example("low", Decimal("0.04"), ()),
            OpportunityRecord.example("high", Decimal("0.08"), ()),
            rejected,
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
    assert body[-1]["rule_versions"] == ["unavailable", "unavailable"]
    assert body[-1]["fee_status"] == "unknown"
    assert body[-1]["quantity"] is None
    assert body[-1]["kalshi_vwap"] is None
    assert body[-1]["polymarket_vwap"] is None
    assert body[-1]["deployed_capital"] is None
    assert body[-1]["profit_floor"] is None
    assert body[-1]["conservative_roi"] is None
    assert body[-1]["book_age_ms"] is None


@pytest.mark.asyncio
async def test_business_api_denies_remote_access() -> None:
    transport = httpx.ASGITransport(app=create_app(ApplicationContainer()))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/opportunities")

    assert response.status_code == 403
    assert response.json()["detail"] == "local access only"
