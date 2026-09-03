from decimal import Decimal

import pytest

from backend.app.services.fees import FeeReconciliationService
from backend.app.services.system_control import (
    InMemoryOpeningControlStore,
    OpeningControlState,
    SystemControl,
)


@pytest.mark.asyncio
async def test_fee_difference_above_one_cent_disables_opening() -> None:
    control = SystemControl(opening_enabled=True)
    service = FeeReconciliationService(control)

    result = await service.compare(estimated=Decimal("0.05"), actual=Decimal("0.061"))

    assert result.matches is False
    assert result.difference == Decimal("0.011")
    assert control.opening_enabled is False
    assert control.reason == "actual fee differs from estimate"


@pytest.mark.asyncio
async def test_fee_difference_at_one_cent_is_accepted() -> None:
    control = SystemControl(opening_enabled=True)
    service = FeeReconciliationService(control)

    result = await service.compare(estimated=Decimal("0.05"), actual=Decimal("0.06"))

    assert result.matches is True
    assert control.opening_enabled is True


@pytest.mark.asyncio
async def test_fee_mismatch_closes_durable_control_inside_submission_guard() -> None:
    control = SystemControl(
        store=InMemoryOpeningControlStore(OpeningControlState(True, "enabled"))
    )
    service = FeeReconciliationService(control)

    async with control.opening_submission_guard() as permission:
        result = await service.compare(
            estimated=Decimal("0.01"),
            actual=Decimal("0.03"),
            submission_permission=permission,
        )
        assert not control.owns_submission_permission(permission)

    assert result.matches is False
    assert control.opening_enabled is False
