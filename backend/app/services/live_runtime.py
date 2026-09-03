import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from backend.app.domain.enums import ExecutionState, MappingStatus, Venue
from backend.app.domain.market import BookLevel, NormalizedBook
from backend.app.services.capital import CapitalLedger
from backend.app.services.executable_pairs import ExecutablePair, ExecutablePairService
from backend.app.services.execution import (
    ControlledExecutionService,
    ExecutionAuthorizationService,
    ExecutionEvidence,
    ExecutionRecord,
    ExecutionStore,
    ExecutionSubmissionClaimed,
    ExecutionSupervisorPort,
    ExecutionTradingPort,
)
from backend.app.services.integration_config import (
    IntegrationConfigService,
    RuntimeBundle,
)
from backend.app.services.opportunities import (
    InMemoryOpportunityStore,
    OpportunityRecord,
)
from backend.app.services.optimizer import QuoteOptimizer, QuotePolicy
from backend.app.services.orderbooks import BookSynchronizationError, synchronize_books
from backend.app.services.runtime_status import RuntimeStatusService
from backend.app.services.settings import RiskPolicy, RiskPolicyStore
from backend.app.services.system_control import SystemControl


class NativeMarketDataPort(Protocol):
    async def get_books(
        self,
        pair: ExecutablePair,
        now: datetime,
    ) -> tuple[NormalizedBook, NormalizedBook]: ...


class BalanceTradingPort(ExecutionTradingPort, Protocol):
    async def get_available_balance(self) -> Decimal: ...


MarketDataFactory = Callable[
    [RuntimeBundle],
    NativeMarketDataPort | Awaitable[NativeMarketDataPort],
]
TradingPortsFactory = Callable[
    [RuntimeBundle],
    dict[Venue, BalanceTradingPort] | Awaitable[dict[Venue, BalanceTradingPort]],
]


@dataclass(frozen=True, slots=True)
class PairEvaluation:
    opportunity: OpportunityRecord
    quantity: Decimal
    kalshi_limit_price: Decimal
    polymarket_limit_price: Decimal
    kalshi_reserved_amount: Decimal
    polymarket_reserved_amount: Decimal
    conservative_roi: Decimal
    book_sequences: tuple[str, str]
    balance_versions: tuple[str, str]
    rule_versions: tuple[str, str]
    estimated_fees: tuple[Decimal, Decimal]


class LiveRuntimeService:
    """Evaluates reviewed pairs and submits eligible two-leg FOK orders."""

    def __init__(
        self,
        *,
        integrations: IntegrationConfigService,
        pairs: ExecutablePairService,
        risk_policies: RiskPolicyStore,
        system_control: SystemControl,
        execution_store: ExecutionStore,
        opportunities: InMemoryOpportunityStore,
        runtime_status: RuntimeStatusService,
        market_data_factory: MarketDataFactory,
        trading_ports_factory: TradingPortsFactory,
        optimizer: QuoteOptimizer,
        capital_ledger: CapitalLedger,
        execution_supervisor: ExecutionSupervisorPort | None = None,
    ) -> None:
        self._integrations = integrations
        self._pairs = pairs
        self._risk_policies = risk_policies
        self._system_control = system_control
        self._execution_store = execution_store
        self._opportunities = opportunities
        self._runtime_status = runtime_status
        self._market_data_factory = market_data_factory
        self._trading_ports_factory = trading_ports_factory
        self._optimizer = optimizer
        self._capital_ledger = capital_ledger
        self._execution_supervisor = execution_supervisor
        self._processed_books: set[tuple[str, str, str]] = set()
        self._cycle_lock = asyncio.Lock()

    async def run_once(self, now: datetime) -> int:
        """Run one polling cycle and return the number of executions started."""
        async with self._cycle_lock:
            return await self._run_once(now)

    async def _run_once(self, now: datetime) -> int:
        # Each cycle must take fresh durable snapshots before evaluating a
        # market.  Failures intentionally escape to the runtime loop, which
        # records the error and starts no new opening from stale state.
        await self._system_control.refresh_async()
        policy = await self._risk_policies.refresh()
        # Snapshot recovery candidates before any evaluation can claim a new
        # SUBMITTED record.  A competing runtime that loses that later claim
        # must not reconcile the winner in this same cycle.
        records = await self._execution_store.list_recovery_candidates()
        bundle = await self._integrations.runtime_bundle()
        pairs = await self._pairs.list_executable()
        observed: list[OpportunityRecord] = []
        evaluated: list[tuple[ExecutablePair, PairEvaluation]] = []
        ports: dict[Venue, BalanceTradingPort] | None = None
        if pairs and policy is not None:
            market_data = await _resolve(self._market_data_factory(bundle))
            ports = await _resolve(self._trading_ports_factory(bundle))
            self._bind_emergency_ports(ports)
            for pair in pairs:
                evaluation = await self._evaluate_pair(
                    pair, policy, market_data, ports, now
                )
                if evaluation is None:
                    continue
                observed.append(evaluation.opportunity)
                evaluated.append((pair, evaluation))

        # Publication is the boundary between evaluating the market and acting
        # on it. If it fails, do not reserve capital or submit any orders.
        self._opportunities.replace(observed)

        if records:
            if ports is None:
                ports = await _resolve(self._trading_ports_factory(bundle))
                self._bind_emergency_ports(ports)
            await self._reconcile_persisted_executions(
                records,
                ports,
                policy.maximum_unhedged_loss if policy is not None else Decimal(0),
                now,
            )

        if not pairs:
            self._runtime_status.record_cycle(error="no enabled EXACT market pair")
            return 0
        if policy is None:
            self._runtime_status.record_cycle(error="risk policy is not configured")
            return 0
        assert ports is not None

        executions = 0
        for pair, evaluation in evaluated:
            if evaluation.opportunity.rejection_reasons:
                continue

            identity = (pair.id, *evaluation.book_sequences)
            if identity in self._processed_books:
                continue

            correlation_id = _execution_id(*identity)
            executor = ControlledExecutionService(
                ports,
                self._system_control,
                self._execution_store,
                self._execution_supervisor,
                policy.maximum_unhedged_loss,
            )
            pending_disable_reason: str | None = None
            # Lock order is global submission fence -> execution correlation
            # fence -> capital/venue I/O.  Policy/control writes share the
            # first lock. Recovery uses only the correlation lock for venue
            # queries and performs its control side effects after releasing it.
            async with self._system_control.opening_submission_guard() as permission:
                if not permission.allowed:
                    continue
                fenced_policy = await self._risk_policies.refresh()
                if fenced_policy is None or fenced_policy.version != policy.version:
                    # A PUT won the fence after evaluation.  Re-evaluate on
                    # the next cycle; no stale-policy reservation is created.
                    continue
                async with self._execution_store.execution_guard(
                    correlation_id
                ) as execution_lease:
                    try:
                        await self._execution_store.get(correlation_id)
                    except KeyError:
                        pass
                    else:
                        self._processed_books.add(identity)
                        continue
                    reservation = await self._capital_ledger.reserve_pair(
                        correlation_id,
                        evaluation.kalshi_reserved_amount,
                        evaluation.polymarket_reserved_amount,
                        event_id=pair.id,
                    )
                    evidence = ExecutionEvidence(
                        quote_evaluation_id=str(uuid4()),
                        rule_versions=evaluation.rule_versions,
                        book_sequences=evaluation.book_sequences,
                        balance_versions=evaluation.balance_versions,
                        risk_policy_version=str(policy.version),
                        capital_reservation_id=reservation.evidence_id,
                        quantity=evaluation.quantity,
                        kalshi_market_id=pair.kalshi_market_id,
                        polymarket_market_id=pair.polymarket_market_id,
                        kalshi_outcome=pair.kalshi_outcome,
                        polymarket_outcome=pair.polymarket_outcome,
                        kalshi_limit_price=evaluation.kalshi_limit_price,
                        polymarket_limit_price=evaluation.polymarket_limit_price,
                        conservative_roi=evaluation.conservative_roi,
                        minimum_roi=policy.minimum_roi,
                        estimated_fees=evaluation.estimated_fees,
                    )
                    try:
                        authorization = ExecutionAuthorizationService().issue(
                            MappingStatus.EXACT,
                            evidence,
                            now,
                            correlation_id=correlation_id,
                        )
                        record = await executor.execute(
                            authorization,
                            evidence,
                            now,
                            submission_permission=permission,
                            execution_lease=execution_lease,
                        )
                    except ExecutionSubmissionClaimed:
                        # A concurrent runtime owns this identity and its
                        # reservation.  Do not release or recover it here.
                        continue
                    except BaseException:
                        # Before a durable claim, this runtime owns the only
                        # reservation and must release it. Once claimed, the
                        # recovery record owns the reservation even if a later
                        # transition/save/venue step fails.
                        try:
                            await self._execution_store.get(correlation_id)
                        except KeyError:
                            await self._capital_ledger.release_pair(correlation_id)
                        raise
                    self._processed_books.add(identity)
                    await self._settle_capital(record)
                    pending_disable_reason = executor.take_pending_disable_reason()
                    executions += 1
            if pending_disable_reason is not None:
                await self._system_control.disable_opening_async(pending_disable_reason)

        self._runtime_status.record_cycle(executions=executions)
        return executions

    def _bind_emergency_ports(self, ports: dict[Venue, BalanceTradingPort]) -> None:
        if self._execution_supervisor is not None:
            self._execution_supervisor.bind_emergency_ports(ports)

    async def _reconcile_persisted_executions(
        self,
        records: list[ExecutionRecord],
        ports: dict[Venue, BalanceTradingPort],
        maximum_unhedged_loss: Decimal,
        now: datetime,
    ) -> None:
        executor = ControlledExecutionService(
            ports,
            self._system_control,
            self._execution_store,
            self._execution_supervisor,
            maximum_unhedged_loss,
        )
        for candidate in records:
            # Recovery keeps the correlation fence while it queries venues,
            # but never holds the global submission fence across that I/O.
            # Any OFF/fee/incident side effect happens after this lock exits.
            record_to_supervise: ExecutionRecord | None = None
            pending_disable_reason: str | None = None
            async with self._execution_store.execution_guard(candidate.correlation_id):
                    try:
                        record = await self._execution_store.get(
                            candidate.correlation_id
                        )
                    except KeyError:
                        continue
                    if record.state is ExecutionState.SUBMITTED:
                        record = await executor.recover_submitted(
                            record,
                            now,
                            supervise=False,
                        )
                    if record.state in {
                        ExecutionState.PAIRED,
                        ExecutionState.PARTIALLY_HEDGED,
                        ExecutionState.EXCEPTION,
                        ExecutionState.CANCELLED,
                    }:
                        await self._settle_capital(record)
                    pending_disable_reason = executor.take_pending_disable_reason()
                    record_to_supervise = record
            if pending_disable_reason is not None:
                await self._system_control.disable_opening_async(pending_disable_reason)
            if self._execution_supervisor is not None and record_to_supervise is not None:
                await self._execution_supervisor.finalize(
                    record_to_supervise,
                    record_to_supervise.evidence,
                    now=now,
                    maximum_unhedged_loss=maximum_unhedged_loss,
                )

    async def _settle_capital(self, record: ExecutionRecord) -> None:
        if record.capital_settled:
            return
        if record.state in {ExecutionState.PAIRED, ExecutionState.PARTIALLY_HEDGED}:
            await self._capital_ledger.convert_pair(record.correlation_id)
        else:
            await self._capital_ledger.release_pair(record.correlation_id)
        record.capital_settled = True
        try:
            await self._execution_store.save(record)
        except Exception:
            record.capital_settled = False
            raise

    async def _evaluate_pair(
        self,
        pair: ExecutablePair,
        policy: RiskPolicy,
        market_data: NativeMarketDataPort,
        ports: dict[Venue, BalanceTradingPort],
        now: datetime,
    ) -> PairEvaluation | None:
        if (
            pair.worst_case_settlement_at is not None
            and pair.worst_case_settlement_at
            > now + timedelta(days=policy.maximum_settlement_days)
        ):
            return _rejected_evaluation(
                pair,
                policy,
                now,
                ("SETTLEMENT_TOO_LATE",),
            )

        kalshi_book, polymarket_book = await market_data.get_books(pair, now)
        try:
            books = synchronize_books(
                kalshi_book,
                polymarket_book,
                now=now,
                maximum_age=timedelta(seconds=float(policy.maximum_book_age_seconds)),
                maximum_arrival_gap=timedelta(
                    seconds=float(policy.maximum_arrival_gap_seconds)
                ),
            )
        except BookSynchronizationError as exc:
            return _rejected_evaluation(
                pair,
                policy,
                now,
                (exc.code,),
                kalshi_book=kalshi_book,
                polymarket_book=polymarket_book,
            )
        if not books.kalshi.asks or not books.polymarket.asks:
            return _rejected_evaluation(
                pair,
                policy,
                now,
                ("INSUFFICIENT_DEPTH",),
                kalshi_book=books.kalshi,
                polymarket_book=books.polymarket,
            )

        kalshi_balance, polymarket_balance = await _gather_balances(ports)
        self._capital_ledger.sync_available_balances(
            kalshi_available=kalshi_balance,
            polymarket_available=polymarket_balance,
        )
        await self._capital_ledger.refresh()
        maximum_quantity = min(
            sum((level.quantity for level in books.kalshi.asks), Decimal(0)),
            sum((level.quantity for level in books.polymarket.asks), Decimal(0)),
        )
        quote_result = self._optimizer.optimize(
            mapping_status=pair.status,
            kalshi_category=pair.kalshi_category,
            polymarket_category=pair.polymarket_category,
            kalshi_asks=list(books.kalshi.asks),
            polymarket_asks=list(books.polymarket.asks),
            policy=QuotePolicy(
                minimum_roi=policy.minimum_roi,
                maximum_quantity=maximum_quantity,
                quantity_step=pair.quantity_step,
                explicit_cost=policy.explicit_cost,
                risk_buffer=policy.risk_buffer,
                kalshi_balance=self._capital_ledger.available(Venue.KALSHI),
                polymarket_balance=self._capital_ledger.available(Venue.POLYMARKET),
                per_trade_limit=policy.per_trade_limit,
                per_event_limit=self._capital_ledger.remaining_event_limit(
                    pair.id,
                    policy.per_event_limit,
                ),
                portfolio_limit=self._capital_ledger.remaining_portfolio_limit(
                    policy.portfolio_limit
                ),
            ),
        )
        quote = quote_result.best_quote
        if quote is None:
            return _rejected_evaluation(
                pair,
                policy,
                now,
                quote_result.rejection_reasons,
                kalshi_book=books.kalshi,
                polymarket_book=books.polymarket,
                balance_versions=(str(kalshi_balance), str(polymarket_balance)),
            )
        if quote.quantity < pair.minimum_quantity:
            return _rejected_evaluation(
                pair,
                policy,
                now,
                ("BELOW_MINIMUM_QUANTITY",),
                kalshi_book=books.kalshi,
                polymarket_book=books.polymarket,
                balance_versions=(str(kalshi_balance), str(polymarket_balance)),
            )

        kalshi_limit = _limit_price(books.kalshi.asks, quote.quantity)
        polymarket_limit = _limit_price(books.polymarket.asks, quote.quantity)
        age_ms = int(
            max(
                now - books.kalshi.received_at, now - books.polymarket.received_at
            ).total_seconds()
            * 1000
        )
        opportunity = OpportunityRecord(
            id=pair.id,
            event=pair.title,
            kalshi_outcome=pair.kalshi_outcome,
            polymarket_outcome=pair.polymarket_outcome,
            mapping_status=pair.status.value,
            quantity=quote.quantity,
            kalshi_vwap=quote.kalshi_cost / quote.quantity,
            polymarket_vwap=quote.polymarket_cost / quote.quantity,
            total_fees=quote.kalshi_fee + quote.polymarket_fee,
            deployed_capital=quote.deployed_capital,
            payout=quote.quantity,
            profit_floor=quote.profit_floor,
            conservative_roi=quote.conservative_roi,
            expected_settlement_at=pair.kalshi_expected_settlement_at
            or pair.polymarket_expected_settlement_at
            or now,
            worst_case_settlement_at=pair.worst_case_settlement_at
            or now + timedelta(days=policy.maximum_settlement_days),
            book_age_ms=age_ms,
            rejection_reasons=(),
            rule_versions=(
                _rule_hash(pair.kalshi_rule_text),
                _rule_hash(pair.polymarket_rule_text),
            ),
            book_sequences=(books.kalshi.sequence, books.polymarket.sequence),
            balance_versions=(str(kalshi_balance), str(polymarket_balance)),
            risk_policy_version=str(policy.version),
            fee_status="calculated",
        )
        return PairEvaluation(
            opportunity=opportunity,
            quantity=quote.quantity,
            kalshi_limit_price=kalshi_limit,
            polymarket_limit_price=polymarket_limit,
            kalshi_reserved_amount=quote.kalshi_cost + quote.kalshi_fee,
            polymarket_reserved_amount=quote.polymarket_cost + quote.polymarket_fee,
            conservative_roi=quote.conservative_roi,
            book_sequences=(books.kalshi.sequence, books.polymarket.sequence),
            balance_versions=(str(kalshi_balance), str(polymarket_balance)),
            rule_versions=(
                _rule_hash(pair.kalshi_rule_text),
                _rule_hash(pair.polymarket_rule_text),
            ),
            estimated_fees=(quote.kalshi_fee, quote.polymarket_fee),
        )


async def _resolve[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


async def _gather_balances(
    ports: dict[Venue, BalanceTradingPort],
) -> tuple[Decimal, Decimal]:
    # Balances are read immediately before authorization so stale account data
    # cannot be reused from an earlier polling cycle.
    import asyncio

    kalshi, polymarket = await asyncio.gather(
        ports[Venue.KALSHI].get_available_balance(),
        ports[Venue.POLYMARKET].get_available_balance(),
    )
    return kalshi, polymarket


def _limit_price(levels: tuple[BookLevel, ...], quantity: Decimal) -> Decimal:
    remaining = quantity
    for level in levels:
        remaining -= min(remaining, level.quantity)
        if remaining == 0:
            return level.price
    raise ValueError("insufficient depth for limit price")


def _rule_hash(rule_text: str) -> str:
    return f"sha256:{sha256(rule_text.encode('utf-8')).hexdigest()}"


def _execution_id(pair_id: str, kalshi_sequence: str, polymarket_sequence: str) -> str:
    identity = f"live-execution:{pair_id}:{kalshi_sequence}:{polymarket_sequence}"
    return str(uuid5(NAMESPACE_URL, identity))


def _rejected_evaluation(
    pair: ExecutablePair,
    policy: RiskPolicy,
    now: datetime,
    reasons: tuple[str, ...],
    *,
    kalshi_book: NormalizedBook | None = None,
    polymarket_book: NormalizedBook | None = None,
    balance_versions: tuple[str, str] = ("unavailable", "unavailable"),
) -> PairEvaluation:
    books = tuple(book for book in (kalshi_book, polymarket_book) if book is not None)
    age_ms = max(
        (int((now - book.received_at).total_seconds() * 1000) for book in books),
        default=0,
    )
    opportunity = OpportunityRecord(
        id=pair.id,
        event=pair.title,
        kalshi_outcome=pair.kalshi_outcome,
        polymarket_outcome=pair.polymarket_outcome,
        mapping_status=pair.status.value,
        quantity=Decimal(0),
        kalshi_vwap=Decimal(0),
        polymarket_vwap=Decimal(0),
        total_fees=Decimal(0),
        deployed_capital=Decimal(0),
        payout=Decimal(0),
        profit_floor=Decimal(0),
        conservative_roi=Decimal(0),
        expected_settlement_at=pair.kalshi_expected_settlement_at
        or pair.polymarket_expected_settlement_at
        or now,
        worst_case_settlement_at=pair.worst_case_settlement_at
        or now + timedelta(days=policy.maximum_settlement_days),
        book_age_ms=age_ms,
        rejection_reasons=reasons,
        rule_versions=(
            _rule_hash(pair.kalshi_rule_text),
            _rule_hash(pair.polymarket_rule_text),
        ),
        book_sequences=(
            kalshi_book.sequence if kalshi_book is not None else "unavailable",
            polymarket_book.sequence if polymarket_book is not None else "unavailable",
        ),
        balance_versions=balance_versions,
        risk_policy_version=str(policy.version),
        fee_status=(
            "calculated"
            if "FEE_UNKNOWN" not in reasons
            and balance_versions != ("unavailable", "unavailable")
            else "unknown"
        ),
    )
    return PairEvaluation(
        opportunity=opportunity,
        quantity=Decimal(0),
        kalshi_limit_price=Decimal(0),
        polymarket_limit_price=Decimal(0),
        kalshi_reserved_amount=Decimal(0),
        polymarket_reserved_amount=Decimal(0),
        conservative_roi=Decimal(0),
        book_sequences=(
            kalshi_book.sequence if kalshi_book is not None else "unavailable",
            polymarket_book.sequence if polymarket_book is not None else "unavailable",
        ),
        balance_versions=("unavailable", "unavailable"),
        rule_versions=(
            _rule_hash(pair.kalshi_rule_text),
            _rule_hash(pair.polymarket_rule_text),
        ),
        estimated_fees=(Decimal(0), Decimal(0)),
    )
