from datetime import datetime, timedelta

import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.security import Principal, Role, get_current_principal
from backend.app.main import create_app
from backend.app.services.settings import InMemoryRiskPolicyStore, RiskPolicyInput


@pytest.mark.asyncio
async def test_settings_update_creates_new_version() -> None:
    container = ApplicationContainer()
    assert container.risk_policies.current is not None
    original_version = str(container.risk_policies.current.version)
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "operator",
        frozenset({Role.OPERATOR}),
    )
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
    assert response.json()["maximum_arrival_gap_seconds"] == "0.5"
    assert response.json()["version"] != original_version


@pytest.mark.asyncio
async def test_risk_policy_update_is_returned_as_the_active_policy_with_utc_timestamp() -> None:
    container = ApplicationContainer()
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "operator",
        frozenset({Role.OPERATOR}),
    )
    payload = {
        "minimum_roi": "0.08",
        "maximum_settlement_days": 14,
        "maximum_book_age_seconds": "1.25",
        "per_trade_limit": "9",
        "per_event_limit": "19",
        "portfolio_limit": "49",
        "explicit_cost": "0.01",
        "risk_buffer": "0.2",
        "maximum_unhedged_seconds": "1.5",
        "maximum_unhedged_loss": "1.25",
        "maximum_arrival_gap_seconds": "0.25",
    }
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        updated = await client.put("/api/settings/risk", json=payload)
        active = await client.get("/api/settings/risk")

    assert updated.status_code == 200
    assert active.status_code == 200
    updated_policy = updated.json()
    active_policy = active.json()
    assert active_policy == updated_policy
    assert {key: active_policy[key] for key in payload} == payload
    created_at = datetime.fromisoformat(active_policy["created_at"])
    assert created_at.tzinfo is not None
    assert created_at.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_get_risk_policy_refreshes_a_stale_process_cache() -> None:
    durable = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())
    stale = durable.current
    updated_input = RiskPolicyInput.defaults()
    latest = await durable.create(updated_input)

    class StaleCacheStore:
        current = stale

        async def refresh(self):
            return latest

    container = ApplicationContainer()
    container.risk_policies = StaleCacheStore()  # type: ignore[assignment]
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "viewer", frozenset({Role.VIEWER})
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/settings/risk")

    assert response.status_code == 200
    assert response.json()["version"] == str(latest.version)


@pytest.mark.asyncio
async def test_viewer_cannot_update_risk_policy() -> None:
    container = ApplicationContainer()
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "viewer",
        frozenset({Role.VIEWER}),
    )
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
                "maximum_unhedged_loss": "2",
            },
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_negative_risk_limit_is_rejected() -> None:
    container = ApplicationContainer()
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "operator",
        frozenset({Role.OPERATOR}),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/settings/risk",
            json={
                "minimum_roi": "0.05",
                "maximum_settlement_days": 20,
                "maximum_book_age_seconds": "1.5",
                "per_trade_limit": "-1",
                "per_event_limit": "25",
                "portfolio_limit": "100",
                "explicit_cost": "0",
                "risk_buffer": "0.25",
                "maximum_unhedged_seconds": "2",
                "maximum_unhedged_loss": "2",
            },
        )

    assert response.status_code == 422
