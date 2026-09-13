import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from backend.app.container import ApplicationContainer
from backend.app.domain.enums import MappingStatus, Venue
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
        clock=lambda: NOW,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "captured_offset,received_offset,expected_reasons",
    [
        (0, 0, ()),
        (1, 0, ("BOOK_TIME_IN_FUTURE",)),
        (0, 1, ("BOOK_TIME_IN_FUTURE",)),
        (-5, 0, ("STALE_BOOK",)),
        (0, -5, ("STALE_BOOK",)),
    ],
)
async def test_live_books_use_trusted_time_after_async_fetch(
    captured_offset, received_offset, expected_reasons
):
    current_time = NOW

    class DelayedBooks(Books):
        async def get_books(self, value, now):
            nonlocal current_time
            assert now == NOW
            await asyncio.sleep(0)
            current_time = NOW + timedelta(milliseconds=10)
            k, p = await super().get_books(value, current_time)
            k = replace(
                k,
                captured_at=current_time + timedelta(seconds=captured_offset),
                received_at=current_time + timedelta(seconds=received_offset),
            )
            current_time += timedelta(milliseconds=10)
            return k, p

    class Balance:
        async def get_available_balance(self):
            return Decimal(100)

    container = ApplicationContainer()
    books = DelayedBooks()
    ports = {Venue.KALSHI: Balance(), Venue.POLYMARKET: Balance()}
    runtime = LiveRuntimeService(
        integrations=container.integration_configs,
        pairs=container.executable_pairs,
        risk_policies=container.risk_policies,
        system_control=container.system_control,
        execution_store=container.executions,
        opportunities=container.opportunities,
        runtime_status=container.runtime_status,
        market_data_factory=lambda _: books,
        clock=lambda: current_time,
        trading_ports_factory=lambda _: ports,
        optimizer=optimizer(),
        capital_ledger=container.capital_ledger,
    )
    result = await runtime._evaluate_pair(
        replace(pair(), status=MappingStatus.EXACT),
        await container.risk_policies.initialize(),
        books,
        ports,
        NOW,
    )

    assert result.opportunity.rejection_reasons == expected_reasons
    if not expected_reasons:
        assert result.quantity > 0
        assert result.opportunity.book_age_ms == 10
