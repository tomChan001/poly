from decimal import Decimal

from backend.app.services.fees import FeeReconciliationService
from backend.app.services.system_control import SystemControl


def test_fee_difference_above_one_cent_disables_opening() -> None:
    control = SystemControl(opening_enabled=True)
    service = FeeReconciliationService(control)

    result = service.compare(estimated=Decimal("0.05"), actual=Decimal("0.061"))

    assert result.matches is False
    assert result.difference == Decimal("0.011")
    assert control.opening_enabled is False
    assert control.reason == "actual fee differs from estimate"


def test_fee_difference_at_one_cent_is_accepted() -> None:
    control = SystemControl(opening_enabled=True)
    service = FeeReconciliationService(control)

    result = service.compare(estimated=Decimal("0.05"), actual=Decimal("0.06"))

    assert result.matches is True
    assert control.opening_enabled is True
