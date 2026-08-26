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
    kalshi_balance: Decimal
    polymarket_balance: Decimal
    per_trade_limit: Decimal
    per_event_limit: Decimal
    portfolio_limit: Decimal


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
        try:
            feasible_quantity = self._largest_quantity_within_hard_limits(
                maximum_depth,
                kalshi_category,
                polymarket_category,
                kalshi_asks,
                polymarket_asks,
                policy,
            )
        except KeyError as exc:
            if "FEE_UNKNOWN" in str(exc):
                return OptimizationResult(None, ("FEE_UNKNOWN",))
            raise
        if feasible_quantity > 0:
            candidates = [quantity for quantity in candidates if quantity <= feasible_quantity]
            quantity = policy.quantity_step
            while quantity <= feasible_quantity:
                candidates.append(quantity)
                quantity += policy.quantity_step
            candidates = sorted(set(candidates))
        else:
            candidates = [policy.quantity_step]
        eligible: list[ExecutableQuote] = []
        rejection_reasons: list[str] = []
        for quantity in candidates:
            try:
                quote = self._quote_for_quantity(
                    quantity,
                    kalshi_category,
                    polymarket_category,
                    kalshi_asks,
                    polymarket_asks,
                    policy,
                )
            except KeyError as exc:
                if "FEE_UNKNOWN" in str(exc):
                    return OptimizationResult(None, ("FEE_UNKNOWN",))
                raise

            if quote.kalshi_cost + quote.kalshi_fee > policy.kalshi_balance:
                _append_reason(rejection_reasons, "KALSHI_BALANCE_INSUFFICIENT")
                continue
            if quote.polymarket_cost + quote.polymarket_fee > policy.polymarket_balance:
                _append_reason(rejection_reasons, "POLYMARKET_BALANCE_INSUFFICIENT")
                continue
            if quote.deployed_capital > policy.per_trade_limit:
                _append_reason(rejection_reasons, "PER_TRADE_LIMIT")
                continue
            if quote.deployed_capital > policy.per_event_limit:
                _append_reason(rejection_reasons, "EVENT_LIMIT")
                continue
            if quote.deployed_capital > policy.portfolio_limit:
                _append_reason(rejection_reasons, "PORTFOLIO_LIMIT")
                continue
            if quote.conservative_roi >= policy.minimum_roi:
                eligible.append(quote)
                continue
            _append_reason(rejection_reasons, "ROI_BELOW_THRESHOLD")

        if not eligible:
            return OptimizationResult(None, tuple(rejection_reasons or ["ROI_BELOW_THRESHOLD"]))
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

    def _largest_quantity_within_hard_limits(
        self,
        maximum: Decimal,
        kalshi_category: str,
        polymarket_category: str,
        kalshi_asks: list[BookLevel],
        polymarket_asks: list[BookLevel],
        policy: QuotePolicy,
    ) -> Decimal:
        low = 0
        high = int(maximum / policy.quantity_step)
        while low < high:
            midpoint = (low + high + 1) // 2
            quantity = policy.quantity_step * midpoint
            quote = self._quote_for_quantity(
                quantity,
                kalshi_category,
                polymarket_category,
                kalshi_asks,
                polymarket_asks,
                policy,
            )
            within_limits = (
                quote.kalshi_cost + quote.kalshi_fee <= policy.kalshi_balance
                and quote.polymarket_cost + quote.polymarket_fee
                <= policy.polymarket_balance
                and quote.deployed_capital <= policy.per_trade_limit
                and quote.deployed_capital <= policy.per_event_limit
                and quote.deployed_capital <= policy.portfolio_limit
            )
            if within_limits:
                low = midpoint
            else:
                high = midpoint - 1
        return policy.quantity_step * low


def _append_reason(reasons: list[str], code: str) -> None:
    if code not in reasons:
        reasons.append(code)
