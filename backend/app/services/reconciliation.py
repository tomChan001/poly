from dataclasses import dataclass
from decimal import Decimal

from backend.app.domain.enums import Venue
from backend.app.services.system_control import SystemControl


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    venue: Venue
    available_balance: Decimal
    open_order_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    matches: bool
    differences: tuple[str, ...]


class ReconciliationService:
    def __init__(self, system_control: SystemControl) -> None:
        self._control = system_control

    def compare(
        self,
        local: AccountSnapshot,
        platform: AccountSnapshot,
    ) -> ReconciliationResult:
        if local.venue is not platform.venue:
            raise ValueError("cannot reconcile different venues")

        differences: list[str] = []
        if local.available_balance != platform.available_balance:
            differences.append("balance")
        if local.open_order_ids != platform.open_order_ids:
            differences.append("open_orders")
        if differences:
            self._control.disable_opening(
                f"reconciliation mismatch on {local.venue}: {', '.join(differences)}"
            )
        return ReconciliationResult(not differences, tuple(differences))

