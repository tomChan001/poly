from dataclasses import dataclass
from decimal import Decimal

from backend.app.domain.enums import Venue


@dataclass(frozen=True, slots=True)
class ShadowOrderRequest:
    venue: Venue
    quantity: Decimal
    limit_price: Decimal


@dataclass(frozen=True, slots=True)
class ShadowFill:
    venue: Venue
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True, slots=True)
class ShadowExecutionResult:
    correlation_id: str
    fills: tuple[ShadowFill, ...]
    matched_quantity: Decimal
    network_requests: int


class ShadowExecutionService:
    async def execute(
        self,
        correlation_id: str,
        requests: tuple[ShadowOrderRequest, ShadowOrderRequest],
    ) -> ShadowExecutionResult:
        fills = tuple(
            ShadowFill(request.venue, request.quantity, request.limit_price)
            for request in requests
        )
        matched = min(fill.quantity for fill in fills)
        # Shadow mode is structurally incapable of obtaining an authenticated
        # client, so a configuration mistake cannot leak a real order.
        return ShadowExecutionResult(correlation_id, fills, matched, network_requests=0)

