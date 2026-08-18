from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from backend.app.domain.enums import Venue


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


class FeeEngine:
    def __init__(self) -> None:
        self._rules: dict[tuple[Venue, str], ProbabilityCurveFeeRule] = {}

    def register(
        self,
        venue: Venue,
        market_category: str,
        rule: ProbabilityCurveFeeRule,
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

