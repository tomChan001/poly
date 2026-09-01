from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.security import Principal, Role, get_current_principal
from backend.app.main import create_app
from backend.app.services.executable_pairs import ExecutablePairInput
from backend.app.services.mappings import REQUIRED_REVIEW_ITEMS


def reviewed_app(container: ApplicationContainer):
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "local-reviewer",
        frozenset({Role.OPERATOR, Role.REVIEWER, Role.VIEWER}),
    )
    return app


PAIR = {
    "title": "Example complementary market",
    "kalshi_market_id": "KXEXAMPLE-26",
    "kalshi_outcome": "no",
    "kalshi_rule_text": "Kalshi pays NO unless event X occurs before deadline.",
    "kalshi_rule_url": "https://kalshi.com/markets/KXEXAMPLE-26",
    "polymarket_market_id": "123456789",
    "polymarket_outcome": "yes",
    "polymarket_rule_text": "Polymarket pays YES when event X does not occur before deadline.",
    "polymarket_rule_url": "https://polymarket.com/event/example",
    "minimum_quantity": "1",
    "quantity_step": "1",
    "enabled": True,
}


@pytest.mark.asyncio
async def test_pair_requires_human_exact_review_before_runtime_use() -> None:
    container = ApplicationContainer()
    await container.executable_pairs.upsert_discovered(
        ExecutablePairInput(
            title=str(PAIR["title"]),
            kalshi_market_id=str(PAIR["kalshi_market_id"]),
            kalshi_outcome=str(PAIR["kalshi_outcome"]),
            kalshi_rule_text=str(PAIR["kalshi_rule_text"]),
            kalshi_rule_url=str(PAIR["kalshi_rule_url"]),
            polymarket_market_id=str(PAIR["polymarket_market_id"]),
            polymarket_outcome=str(PAIR["polymarket_outcome"]),
            polymarket_rule_text=str(PAIR["polymarket_rule_text"]),
            polymarket_rule_url=str(PAIR["polymarket_rule_url"]),
            minimum_quantity=Decimal(1),
            quantity_step=Decimal(1),
            enabled=True,
        ),
        source_candidate_id="oddpool-001",
        source_updated_at=datetime(2026, 8, 21, 2, 0, tzinfo=UTC),
    )
    transport = httpx.ASGITransport(app=reviewed_app(container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        [candidate] = (await client.get("/api/pairs")).json()
        pair_id = candidate["id"]
        reviewed = await client.post(
            f"/api/pairs/{pair_id}/review",
            json={
                "status": "exact",
                "checklist": {item: True for item in REQUIRED_REVIEW_ITEMS},
                "truth_table": [
                    {"kalshi": "1", "polymarket": "0"},
                    {"kalshi": "0", "polymarket": "1"},
                ],
                "notes": "Rules are complementary.",
            },
        )
        listed = await client.get("/api/pairs")

    assert candidate["status"] == "pending_review"
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "exact"
    assert listed.json()[0]["reviewed_by"] == "local-reviewer"
    executable = await container.executable_pairs.list_executable()
    assert [pair.id for pair in executable] == [pair_id]


@pytest.mark.asyncio
async def test_manual_pair_creation_and_editing_are_not_exposed() -> None:
    container = ApplicationContainer()
    app = reviewed_app(container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/pairs", json=PAIR)
        changed = await client.put("/api/pairs/not-manual", json=PAIR)

    assert created.status_code == 405
    assert changed.status_code in {404, 405}
