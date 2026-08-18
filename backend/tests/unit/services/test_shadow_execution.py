from decimal import Decimal

import pytest

from backend.app.domain.enums import Venue
from backend.app.services.shadow_execution import (
    ShadowExecutionService,
    ShadowOrderRequest,
)


@pytest.mark.asyncio
async def test_shadow_execution_matches_both_legs_without_network_access() -> None:
    service = ShadowExecutionService()
    requests = (
        ShadowOrderRequest(Venue.KALSHI, Decimal(10), Decimal("0.70")),
        ShadowOrderRequest(Venue.POLYMARKET, Decimal(10), Decimal("0.20")),
    )

    result = await service.execute("corr-1", requests)

    assert result.matched_quantity == Decimal(10)
    assert result.network_requests == 0

