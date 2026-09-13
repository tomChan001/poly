"""Read-only market screening. A preview never authorizes an order."""

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from backend.app.domain.enums import MappingStatus
from backend.app.domain.market import NormalizedBook
from backend.app.services.executable_pairs import ExecutablePair
from backend.app.services.optimizer import QuoteOptimizer, QuotePolicy
from backend.app.services.orderbooks import BookSynchronizationError, synchronize_books
from backend.app.services.pair_risk import (
    best_ask_quantity,
    liquidity_reasons,
    paired_liquidity,
    settlement_reasons,
)
from backend.app.services.settings import RiskPolicy


class PreviewMarketData(Protocol):
    async def get_books(
        self,
        pair: ExecutablePair,
        now: datetime,
    ) -> tuple[NormalizedBook, NormalizedBook]: ...


class PreviewFeeProvider(Protocol):
    async def optimizer_for(self, pair: ExecutablePair) -> QuoteOptimizer: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class PairPreview:
    eligible: bool = False
    rejection_reasons: tuple[str, ...] = ()
    evaluated_at: datetime
    risk_policy_version: str
    valid_until: datetime | None = None
    conservative_roi: Decimal | None = None
    gross_roi: Decimal | None = None
    quantity: Decimal | None = None
    kalshi_best_ask: Decimal | None = None
    polymarket_best_ask: Decimal | None = None
    kalshi_best_ask_quantity: Decimal | None = None
    polymarket_best_ask_quantity: Decimal | None = None
    paired_liquidity: Decimal | None = None
    kalshi_vwap: Decimal | None = None
    polymarket_vwap: Decimal | None = None
    total_fees: Decimal | None = None
    deployed_capital: Decimal | None = None
    profit_floor: Decimal | None = None


class PairPreviewService:
    def __init__(
        self,
        optimizer: QuoteOptimizer,
        market_data: PreviewMarketData | None = None,
        fee_provider: PreviewFeeProvider | None = None,
        *,
        start_index: int = 0,
    ) -> None:
        self._optimizer = optimizer
        self._market_data = market_data
        self._fee_provider = fee_provider
        self._slots = asyncio.Semaphore(4)
        self._prepared_fees: dict[str, QuoteOptimizer | None] = {}
        self._next_start = start_index

    async def evaluate_many(
        self,
        pairs: list[ExecutablePair],
        policy: RiskPolicy | None,
    ) -> list[PairPreview]:
        if not pairs:
            return []
        # Resolve relatively slow fee metadata before collecting short-lived
        # books. A slow market must not age healthy quotes out of the response.
        order = [(index + self._next_start) % len(pairs) for index in range(len(pairs))]
        self._next_start = (self._next_start + 4) % len(pairs)
        await self._prepare_fees([pairs[index] for index in order])
        tasks = {
            asyncio.create_task(self.evaluate(pairs[index], policy)): index
            for index in order
        }
        pending = set(tasks)
        results = [
            PairPreview(
                evaluated_at=datetime.now(UTC),
                risk_policy_version=str(policy.version) if policy else "",
                rejection_reasons=("PREVIEW_PENDING",),
            )
            for _ in pairs
        ]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 12
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    timeout=max(0, deadline - loop.time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    break
                for task in done:
                    try:
                        preview = task.result()
                    except Exception:  # noqa: BLE001 - one malformed candidate cannot hide all others
                        preview = replace(
                            results[tasks[task]],
                            rejection_reasons=("NATIVE_DATA_UNAVAILABLE",),
                        )
                    results[tasks[task]] = preview
                    if preview.eligible and preview.valid_until is not None:
                        remaining = (
                            preview.valid_until - datetime.now(UTC)
                        ).total_seconds()
                        # Reserve a transit margin; rotate priority across
                        # refreshes so large lists cannot starve later rows.
                        deadline = min(deadline, loop.time() + max(0, remaining * 0.8))
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return results

    async def _prepare_fees(self, pairs: list[ExecutablePair]) -> None:
        provider = self._fee_provider
        if provider is None:
            return

        async def prepare(pair: ExecutablePair) -> None:
            self._prepared_fees[pair.id] = None
            async with self._slots:
                try:
                    self._prepared_fees[pair.id] = await provider.optimizer_for(pair)
                except Exception:  # noqa: BLE001 - unknown fees stay explicitly unknown
                    self._prepared_fees[pair.id] = None

        tasks = [asyncio.create_task(prepare(pair)) for pair in pairs]
        try:
            await asyncio.wait(tasks, timeout=12)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def evaluate(
        self,
        pair: ExecutablePair,
        policy: RiskPolicy | None,
        now: datetime | None = None,
    ) -> PairPreview:
        preview = PairPreview(
            evaluated_at=now or datetime.now(UTC),
            risk_policy_version=str(policy.version) if policy else "",
        )
        if policy is None:
            return replace(preview, rejection_reasons=("RISK_POLICY_UNAVAILABLE",))
        reasons = list(settlement_reasons(pair, policy, preview.evaluated_at))
        if not pair.enabled:
            reasons.append("PAIR_DISABLED")
        if pair.status is MappingStatus.REJECTED:
            reasons.append("MAPPING_REJECTED")
        if self._market_data is None:
            return replace(
                preview, rejection_reasons=tuple(reasons + ["NATIVE_DATA_UNAVAILABLE"])
            )
        optimizer = self._optimizer
        fee_unknown = False
        try:
            if pair.id in self._prepared_fees:
                prepared = self._prepared_fees[pair.id]
                fee_unknown = prepared is None
                optimizer = prepared or optimizer
            elif self._fee_provider is not None:
                async with self._slots:
                    async with asyncio.timeout(12):
                        optimizer = await self._fee_provider.optimizer_for(pair)
        except Exception:  # noqa: BLE001 - unavailable or unsupported public fee metadata
            fee_unknown = True
        try:
            async with self._slots:
                async with asyncio.timeout(12):
                    kalshi, polymarket = await self._market_data.get_books(
                        pair, preview.evaluated_at
                    )
            # Wall time after receipt is essential: slow network responses must
            # not be evaluated against the time before the requests started.
            preview = replace(preview, evaluated_at=now or datetime.now(UTC))
        except Exception:  # noqa: BLE001 - isolate one unavailable market, never publish exception secrets
            return replace(
                preview, rejection_reasons=tuple(reasons + ["NATIVE_DATA_UNAVAILABLE"])
            )

        reasons = list(settlement_reasons(pair, policy, preview.evaluated_at))
        if not pair.enabled:
            reasons.append("PAIR_DISABLED")
        if pair.status is MappingStatus.REJECTED:
            reasons.append("MAPPING_REJECTED")
        maximum_age = timedelta(seconds=float(policy.maximum_book_age_seconds))
        timestamps = [
            time
            for book in (kalshi, polymarket)
            for time in (book.captured_at, book.received_at)
            if time is not None
        ]
        preview = replace(preview, valid_until=min(timestamps) + maximum_age)
        try:
            synchronize_books(
                kalshi,
                polymarket,
                now=preview.evaluated_at,
                maximum_age=maximum_age,
                maximum_arrival_gap=timedelta(
                    seconds=float(policy.maximum_arrival_gap_seconds)
                ),
                require_capture_time=False,
            )
        except BookSynchronizationError as exc:
            reasons.append(exc.code)
        prices = [
            book.asks[0].price if book.asks else None for book in (kalshi, polymarket)
        ]
        preview = replace(
            preview,
            kalshi_best_ask=prices[0],
            polymarket_best_ask=prices[1],
            kalshi_best_ask_quantity=best_ask_quantity(kalshi),
            polymarket_best_ask_quantity=best_ask_quantity(polymarket),
            paired_liquidity=paired_liquidity(kalshi, polymarket),
        )
        reasons.extend(liquidity_reasons(kalshi, polymarket, policy))
        if not kalshi.asks or not polymarket.asks:
            return replace(
                preview,
                rejection_reasons=tuple(
                    dict.fromkeys(reasons + ["INSUFFICIENT_DEPTH"])
                ),
            )
        unit_cost = kalshi.asks[0].price + polymarket.asks[0].price
        if unit_cost > 0:
            preview = replace(preview, gross_roi=(Decimal(1) - unit_cost) / unit_cost)
        if fee_unknown:
            return replace(
                preview,
                rejection_reasons=tuple(dict.fromkeys(reasons + ["FEE_UNKNOWN"])),
            )
        maximum_quantity = min(
            sum((level.quantity for level in kalshi.asks), Decimal(0)),
            sum((level.quantity for level in polymarket.asks), Decimal(0)),
        )
        quote_policy = QuotePolicy(
            minimum_roi=policy.minimum_roi,
            maximum_quantity=maximum_quantity,
            minimum_quantity=pair.minimum_quantity,
            quantity_step=pair.quantity_step,
            explicit_cost=policy.explicit_cost,
            risk_buffer=policy.risk_buffer,
            # This screen applies configured capital limits, not private account
            # balances. Execution independently checks balances and open exposure.
            kalshi_balance=Decimal("Infinity"),
            polymarket_balance=Decimal("Infinity"),
            per_trade_limit=policy.per_trade_limit,
            per_event_limit=policy.per_event_limit,
            portfolio_limit=policy.portfolio_limit,
        )
        try:
            result = optimizer.optimize(
                mapping_status=MappingStatus.EXACT,  # hypothetical complementary payout only
                kalshi_category=pair.kalshi_category,
                polymarket_category=pair.polymarket_category,
                kalshi_asks=list(kalshi.asks),
                polymarket_asks=list(polymarket.asks),
                policy=quote_policy,
            )
            reasons.extend(result.rejection_reasons)
            quote = result.best_quote
            if quote is None and "ROI_BELOW_THRESHOLD" in result.rejection_reasons:
                quote = optimizer.optimize(
                    mapping_status=MappingStatus.EXACT,
                    kalshi_category=pair.kalshi_category,
                    polymarket_category=pair.polymarket_category,
                    kalshi_asks=list(kalshi.asks),
                    polymarket_asks=list(polymarket.asks),
                    policy=replace(quote_policy, minimum_roi=Decimal("-Infinity")),
                ).best_quote
        except KeyError:
            # The optimizer may encounter an unknown fee during its quantity
            # search before reaching the normal structured refusal path.
            reasons.append("FEE_UNKNOWN")
            quote = None
        if quote is not None:
            preview = replace(
                preview,
                quantity=quote.quantity,
                conservative_roi=quote.conservative_roi,
                kalshi_vwap=quote.kalshi_cost / quote.quantity,
                polymarket_vwap=quote.polymarket_cost / quote.quantity,
                total_fees=quote.kalshi_fee + quote.polymarket_fee,
                deployed_capital=quote.deployed_capital,
                profit_floor=quote.profit_floor,
            )
            if quote.quantity < pair.minimum_quantity:
                reasons.append("BELOW_MINIMUM_QUANTITY")
        return replace(
            preview,
            eligible=not reasons,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
        )


def expire_preview(preview: PairPreview, now: datetime) -> PairPreview:
    if preview.valid_until is not None and now > preview.valid_until:
        return replace(
            preview,
            eligible=False,
            rejection_reasons=tuple(
                dict.fromkeys((*preview.rejection_reasons, "STALE_BOOK"))
            ),
        )
    return preview
