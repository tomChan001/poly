from decimal import Decimal

import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.security import Principal, Role, get_current_principal
from backend.app.domain.enums import MappingStatus
from backend.app.main import create_app
from backend.app.services.mappings import REQUIRED_REVIEW_ITEMS


@pytest.mark.asyncio
async def test_mapping_review_api_approves_complete_exact_mapping() -> None:
    container = ApplicationContainer()
    kalshi = container.rules.record_version("K", "rule K", "https://kalshi/rule")
    poly = container.rules.record_version("P", "rule P", "https://poly/rule")
    mapping = container.rule_store.create_mapping(
        "K",
        "P",
        kalshi.id,
        poly.id,
        MappingStatus.PENDING_REVIEW,
    )
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "oidc-reviewer",
        frozenset({Role.REVIEWER}),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/mappings/{mapping.id}/review",
            json={
                "status": "exact",
                "reviewer": "forged-reviewer@example.com",
                "checklist": dict.fromkeys(REQUIRED_REVIEW_ITEMS, True),
                "truth_table": [
                    {"kalshi": str(Decimal(1)), "polymarket": str(Decimal(0))},
                    {"kalshi": str(Decimal(0)), "polymarket": str(Decimal(1))},
                ],
                "notes": "Rules are complementary.",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "exact"
    assert response.json()["reviewer"] == "oidc-reviewer"
    assert container.rule_store.mappings[mapping.id].status is MappingStatus.EXACT


@pytest.mark.asyncio
async def test_viewer_cannot_review_mapping() -> None:
    container = ApplicationContainer()
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "viewer",
        frozenset({Role.VIEWER}),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/mappings/00000000-0000-0000-0000-000000000000/review",
            json={
                "status": "rejected",
                "reviewer": "viewer",
                "checklist": {},
                "truth_table": [],
            },
        )

    assert response.status_code == 403
