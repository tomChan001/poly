from decimal import Decimal

from backend.app.services.settings import InMemoryRiskPolicyStore, RiskPolicyInput


def test_risk_policy_updates_create_immutable_versions() -> None:
    store = InMemoryRiskPolicyStore()
    first = store.create(RiskPolicyInput.defaults())
    second_input = RiskPolicyInput.defaults()
    second_input.minimum_roi = Decimal("0.05")

    second = store.create(second_input)

    assert first.version != second.version
    assert first.minimum_roi == Decimal("0.03")
    assert second.minimum_roi == Decimal("0.05")
    assert store.get(first.version) is first

