from dataclasses import replace
from decimal import Decimal

import pytest

from backend.app.container import ApplicationContainer
from backend.app.services.live_runtime import LiveRuntimeService
from backend.tests.unit.services.test_pair_previews import NOW, Books, optimizer, pair


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_date,liquidity,reason",
    [
        (True, "1", "SETTLEMENT_UNKNOWN"),
        (False, "21", "INSUFFICIENT_LIQUIDITY"),
    ],
)
async def test_execution_uses_same_market_risk_gates_before_reading_balances(
    missing_date, liquidity, reason
):
    container = ApplicationContainer()
    runtime = LiveRuntimeService(
        integrations=container.integration_configs,
        pairs=container.executable_pairs,
        risk_policies=container.risk_policies,
        system_control=container.system_control,
        execution_store=container.executions,
        opportunities=container.opportunities,
        runtime_status=container.runtime_status,
        market_data_factory=lambda _: Books(),
        trading_ports_factory=lambda _: {},
        optimizer=optimizer(),
        capital_ledger=container.capital_ledger,
    )
    value = pair()
    if missing_date:
        value = replace(value, polymarket_expected_settlement_at=None)
    policy = replace(
        await container.risk_policies.initialize(),
        minimum_liquidity_contracts=Decimal(liquidity),
    )
    result = await runtime._evaluate_pair(value, policy, Books(), {}, NOW)
    assert result.opportunity.rejection_reasons == (reason,)
    assert result.quantity == 0
