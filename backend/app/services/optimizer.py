from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from backend.app.domain.enums import MappingStatus, Venue
from backend.app.domain.market import BookLevel
from backend.app.services.fees import FeeEngine


class InsufficientDepth(ValueError):
    def __init__(self, missing_quantity: Decimal) -> None:
        super().__init__(f"insufficient depth; missing {missing_quantity}")
        self.missing_quantity = missing_quantity


@dataclass(frozen=True, slots=True)
class QuotePolicy:
    minimum_roi: Decimal
    maximum_quantity: Decimal
    quantity_step: Decimal
    explicit_cost: Decimal
    risk_buffer: Decimal


@dataclass(frozen=True, slots=True)
class ExecutableQuote:
    quantity: Decimal
    kalshi_cost: Decimal
    kalshi_fee: Decimal
    polymarket_cost: Decimal
    polymarket_fee: Decimal
    deployed_capital: Decimal
    profit_floor: Decimal
    conservative_roi: Decimal


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    best_quote: ExecutableQuote | None
    rejection_reasons: tuple[str, ...]


def sweep_cost(levels: list[BookLevel], quantity: Decimal) -> Decimal:
    remaining = quantity
    notional = Decimal(0)
    for level in levels:
        take = min(remaining, level.quantity)
        notional += take * level.price
        remaining -= take
        if remaining == 0:
            break
    if remaining > 0:
        raise InsufficientDepth(remaining)
    return notional


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("quantity step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


class QuoteOptimizer:
    def __init__(self, fees: FeeEngine) -> None:
        self._fees = fees

    @staticmethod
    def from_filled_costs(
        *,
        quantity: Decimal,
        kalshi_cost: Decimal,
        kalshi_fee: Decimal,
        polymarket_cost: Decimal,
        polymarket_fee: Decimal,
        explicit_cost: Decimal,
        risk_buffer: Decimal,
    ) -> ExecutableQuote:
        deployed = (
            kalshi_cost
            + kalshi_fee
            + polymarket_cost
            + polymarket_fee
            + explicit_cost
        )
        profit = quantity - deployed - risk_buffer
        roi = profit / deployed if deployed > 0 else Decimal(0)
        return ExecutableQuote(
            quantity,
            kalshi_cost,
            kalshi_fee,
            polymarket_cost,
            polymarket_fee,
            deployed,
            profit,
            roi,
        )

    def optimize(
        self,
        *,
        mapping_status: MappingStatus,
        kalshi_category: str,
        polymarket_category: str,
        kalshi_asks: list[BookLevel],
        polymarket_asks: list[BookLevel],
        policy: QuotePolicy,
    ) -> OptimizationResult:
        if mapping_status is not MappingStatus.EXACT:
            return OptimizationResult(None, ("MAPPING_NOT_EXACT",))

        maximum_depth = min(
            sum((level.quantity for level in kalshi_asks), Decimal(0)),
            sum((level.quantity for level in polymarket_asks), Decimal(0)),
            policy.maximum_quantity,
        )
        maximum_depth = _round_down(maximum_depth, policy.quantity_step)
        if maximum_depth <= 0:
            return OptimizationResult(None, ("INSUFFICIENT_DEPTH",))

        candidates = self._candidate_quantities(
            kalshi_asks,
            polymarket_asks,
            maximum_depth,
            policy.quantity_step,
        )
        eligible: list[ExecutableQuote] = []
        for quantity in candidates:
            quote = self._quote_for_quantity(
                quantity,
                kalshi_category,
                polymarket_category,
                kalshi_asks,
                polymarket_asks,
                policy,
            )
            if quote.conservative_roi >= policy.minimum_roi:
                eligible.append(quote)

        if not eligible:
            return OptimizationResult(None, ("ROI_BELOW_THRESHOLD",))
        best = max(eligible, key=lambda quote: (quote.profit_floor, quote.conservative_roi))
        return OptimizationResult(best, ())

    @staticmethod
    def _candidate_quantities(
        kalshi_asks: list[BookLevel],
        polymarket_asks: list[BookLevel],
        maximum: Decimal,
        step: Decimal,
    ) -> list[Decimal]:
        cumulative = Decimal(0)
        breakpoints: set[Decimal] = {maximum}
        for levels in (kalshi_asks, polymarket_asks):
            cumulative = Decimal(0)
            for level in levels:
                cumulative += level.quantity
                candidate = _round_down(min(cumulative, maximum), step)
                if candidate > 0:
                    breakpoints.add(candidate)
        return sorted(breakpoints)

    def _quote_for_quantity(
        self,
        quantity: Decimal,
        kalshi_category: str,
        polymarket_category: str,
        kalshi_asks: list[BookLevel],
        polymarket_asks: list[BookLevel],
        policy: QuotePolicy,
    ) -> ExecutableQuote:
        kalshi_cost = sweep_cost(kalshi_asks, quantity)
        polymarket_cost = sweep_cost(polymarket_asks, quantity)
        kalshi_vwap = kalshi_cost / quantity
        polymarket_vwap = polymarket_cost / quantity
        kalshi_fee = self._fees.estimate(
            Venue.KALSHI,
            kalshi_category,
            quantity,
            kalshi_vwap,
        ).amount
        polymarket_fee = self._fees.estimate(
            Venue.POLYMARKET,
            polymarket_category,
            quantity,
            polymarket_vwap,
        ).amount
        return self.from_filled_costs(
            quantity=quantity,
            kalshi_cost=kalshi_cost,
            kalshi_fee=kalshi_fee,
            polymarket_cost=polymarket_cost,
            polymarket_fee=polymarket_fee,
            explicit_cost=policy.explicit_cost,
            risk_buffer=policy.risk_buffer,
        )
