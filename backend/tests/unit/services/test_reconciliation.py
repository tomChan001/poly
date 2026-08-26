from decimal import Decimal

import pytest

from backend.app.domain.enums import Venue
from backend.app.services.reconciliation import AccountSnapshot, ReconciliationService
from backend.app.services.system_control import SystemControl


@pytest.mark.asyncio
async def test_reconciliation_difference_disables_opening() -> None:
    control = SystemControl()
    service = ReconciliationService(control)
    local = AccountSnapshot(Venue.KALSHI, Decimal(100), frozenset({"order-1"}))
    platform = AccountSnapshot(Venue.KALSHI, Decimal(99), frozenset())

    result = await service.compare(local, platform)

    assert result.matches is False
    assert control.opening_enabled is False
    assert "balance" in result.differences
    assert "open_orders" in result.differences
