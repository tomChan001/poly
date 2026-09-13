from dataclasses import FrozenInstanceError, replace
from datetime import UTC
from decimal import Decimal

import pytest

from backend.app.services.settings import InMemoryRiskPolicyStore, RiskPolicyInput


def test_risk_policy_constructor_makes_seed_current_synchronously() -> None:
    store = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())

    assert store.current is not None
    assert store.current.minimum_roi == Decimal("0.03")
    assert store.current.created_at.tzinfo is UTC
    assert store.current.minimum_liquidity_contracts == Decimal(1)


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "-Infinity"])
def test_risk_policy_input_requires_positive_finite_liquidity(value: str) -> None:
    with pytest.raises(ValueError, match="minimum_liquidity_contracts"):
        replace(RiskPolicyInput.defaults(), minimum_liquidity_contracts=Decimal(value))


@pytest.mark.asyncio
async def test_liquidity_policy_versions_keep_their_original_values() -> None:
    store = InMemoryRiskPolicyStore()
    first = await store.initialize()
    value = replace(RiskPolicyInput.defaults(), minimum_liquidity_contracts=Decimal("25.5"))
    second = await store.create(value)
    value.minimum_liquidity_contracts = Decimal(50)

    assert (await store.get(first.version)).minimum_liquidity_contracts == Decimal(1)
    assert (await store.get(second.version)).minimum_liquidity_contracts == Decimal("25.5")
    with pytest.raises(FrozenInstanceError):
        second.minimum_liquidity_contracts = Decimal(50)


@pytest.mark.asyncio
async def test_mutated_invalid_liquidity_input_cannot_create_policy() -> None:
    store = InMemoryRiskPolicyStore()
    first = await store.initialize()
    value = RiskPolicyInput.defaults()
    value.minimum_liquidity_contracts = Decimal("NaN")

    with pytest.raises(ValueError, match="minimum_liquidity_contracts"):
        await store.create(value)
    assert store.current is first


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


@pytest.mark.asyncio
async def test_risk_policy_refresh_returns_current_version_without_creating_one() -> None:
    store = InMemoryRiskPolicyStore()

    assert await store.refresh() is None

    created = await store.initialize()
    assert await store.refresh() is created

