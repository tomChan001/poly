from datetime import UTC
from decimal import Decimal

import pytest

from backend.app.services.settings import InMemoryRiskPolicyStore, RiskPolicyInput


@pytest.mark.asyncio
async def test_risk_policy_updates_create_immutable_versions() -> None:
    store = InMemoryRiskPolicyStore()
    first = await store.initialize()
    second_input = RiskPolicyInput.defaults()
    second_input.minimum_roi = Decimal("0.05")

    second = await store.create(second_input)

    assert first.version != second.version
    assert first.minimum_roi == Decimal("0.03")
    assert second.minimum_roi == Decimal("0.05")
    assert await store.get(first.version) is first
    assert first.created_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_risk_policy_initialize_reuses_the_current_version() -> None:
    store = InMemoryRiskPolicyStore()

    first = await store.initialize()
    second = await store.initialize()

    assert second is first
    assert second.version == first.version

