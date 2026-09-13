from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Protocol

from backend.app.domain.enums import Venue
from backend.app.services.system_control import (
    OpeningSubmissionPermission,
    SystemControl,
)


@dataclass(frozen=True, slots=True)
class FeeEstimate:
    amount: Decimal
    rule_version: str


@dataclass(frozen=True, slots=True)
class ProbabilityCurveFeeRule:
    version: str
    rate: Decimal
    minimum_currency_unit: Decimal = Decimal("0.01")

    def estimate(self, quantity: Decimal, price: Decimal) -> FeeEstimate:
        raw_fee = quantity * self.rate * price * (Decimal(1) - price)
        if raw_fee == 0:
            return FeeEstimate(Decimal(0), self.version)
        rounded = raw_fee.quantize(self.minimum_currency_unit, rounding=ROUND_CEILING)
        return FeeEstimate(rounded, self.version)


class FeeRule(Protocol):
    def estimate(self, quantity: Decimal, price: Decimal) -> FeeEstimate: ...


@dataclass(frozen=True, slots=True)
class KalshiTakerFeeRule:
    """Single equivalent-fill estimate including subcent balance alignment.

    Actual split fills can differ because the exchange rounds each trade fee
    and applies accumulated rounding rebates. This is a preview estimate.
    https://docs.kalshi.com/getting_started/fee_rounding
    """

    version: str
    rate: Decimal

    def estimate(self, quantity: Decimal, price: Decimal) -> FeeEstimate:
        raw_fee = quantity * self.rate * price * (Decimal(1) - price)
        trade_fee = raw_fee.quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
        cost = quantity * price
        aligned_total = (cost + trade_fee).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        return FeeEstimate(aligned_total - cost, self.version)


class FeeEngine:
    def __init__(self) -> None:
        self._rules: dict[tuple[Venue, str], FeeRule] = {}

    def register(
        self,
        venue: Venue,
        market_category: str,
        rule: FeeRule,
    ) -> None:
        self._rules[(venue, market_category)] = rule

    def estimate(
        self,
        venue: Venue,
        market_category: str,
        quantity: Decimal,
        price: Decimal,
    ) -> FeeEstimate:
        try:
            rule = self._rules[(venue, market_category)]
        except KeyError as exc:
            raise KeyError(f"FEE_UNKNOWN: {venue}/{market_category}") from exc
        return rule.estimate(quantity, price)


@dataclass(frozen=True, slots=True)
class FeeReconciliationResult:
    matches: bool
    difference: Decimal


class FeeReconciliationService:
    def __init__(
        self,
        system_control: SystemControl,
        tolerance: Decimal = Decimal("0.01"),
    ) -> None:
        self._system_control = system_control
        self._tolerance = tolerance

    async def compare(
        self,
        estimated: Decimal,
        actual: Decimal,
        *,
        submission_permission: OpeningSubmissionPermission | None = None,
    ) -> FeeReconciliationResult:
        difference = abs(actual - estimated)
        matches = difference <= self._tolerance
        if not matches:
            if submission_permission is None:
                await self._system_control.disable_opening_async(
                    "actual fee differs from estimate"
                )
            else:
                await self._system_control.disable_opening_with_permission(
                    submission_permission,
                    "actual fee differs from estimate",
                )
        return FeeReconciliationResult(matches, difference)
