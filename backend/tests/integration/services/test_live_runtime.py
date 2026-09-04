import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import pytest

from backend.app.core.secrets import InMemorySecretStore
from backend.app.domain.enums import ExecutionState, MappingStatus, Venue
from backend.app.domain.market import BookLevel, NormalizedBook
from backend.app.services.capital import CapitalLedger
from backend.app.services.executable_pairs import (
    ExecutablePairInput,
    ExecutablePairService,
    InMemoryExecutablePairRepository,
)
from backend.app.services.execution import (
    ExecutionEvidence,
    ExecutionRecord,
    FillReport,
    InMemoryExecutionStore,
    OrderOutcomeUnknown,
    OrderRequest,
    OrderStatus,
    OrderSubmissionResult,
)
from backend.app.services.fees import FeeEngine, ProbabilityCurveFeeRule
from backend.app.services.integration_config import (
    ODDPOOL_BASE_URL,
    InMemoryIntegrationConfigRepository,
    IntegrationConfigService,
    IntegrationEnvironment,
    IntegrationProvider,
)
from backend.app.services.live_runtime import BalanceTradingPort, LiveRuntimeService
from backend.app.services.mappings import REQUIRED_REVIEW_ITEMS
from backend.app.services.opportunities import (
    InMemoryOpportunityStore,
    OpportunityRecord,
)
from backend.app.services.optimizer import QuoteOptimizer
from backend.app.services.runtime_status import RuntimeStatusService
from backend.app.services.settings import InMemoryRiskPolicyStore, RiskPolicyInput
from backend.app.services.system_control import (
    InMemoryOpeningControlStore,
    OpeningControlState,
    SystemControl,
)

NOW = datetime(2026, 8, 19, 2, 0, tzinfo=UTC)
PRIVATE_KEY = "0x59c6995e998f97a5a0044966f094538c5f7d2b32a3d47ec9d7c2b4e1f7e3b5d1"
OWNER_ADDRESS = "0xeC165c363b4fB6888BD058c6bB1269c77C9b8E81"


class FakeMarketData:
    async def get_books(self, _pair, now: datetime):
        return (
            NormalizedBook(
                "K-MARKET",
                "no",
                "k-seq-1",
                now,
                now,
                (BookLevel(Decimal("0.70"), Decimal(20)),),
            ),
            NormalizedBook(
                "P-TOKEN",
                "yes",
                "p-seq-1",
                now,
                now,
                (BookLevel(Decimal("0.20"), Decimal(20)),),
            ),
        )


class FakeTradingPort:
    def __init__(self, venue: Venue) -> None:
        self.venue = venue
        self.submissions = 0

    async def get_available_balance(self) -> Decimal:
        return Decimal(100)

    async def submit_fok(self, request: OrderRequest) -> OrderSubmissionResult:
        self.submissions += 1
        return OrderSubmissionResult(
            request.client_order_id,
            OrderStatus.FILLED,
            (
                FillReport(
                    f"{self.venue.value}-fill",
                    request.quantity,
                    request.limit_price,
                    Decimal(0),
                ),
            ),
        )

    async def find_by_client_order_id(self, _client_order_id: str):
        return None


class YieldingTradingPort(FakeTradingPort):
    async def submit_fok(self, request: OrderRequest) -> OrderSubmissionResult:
        await asyncio.sleep(0)
        return await super().submit_fok(request)


class ConcurrentMarketData(FakeMarketData):
    def __init__(self) -> None:
        self.active_calls = 0
        self.maximum_active_calls = 0

    async def get_books(self, pair, now: datetime):
        self.active_calls += 1
        self.maximum_active_calls = max(self.maximum_active_calls, self.active_calls)
        try:
            await asyncio.sleep(0)
            return await super().get_books(pair, now)
        finally:
            self.active_calls -= 1


class RetryableRecoveryPort(FakeTradingPort):
    def __init__(self, venue: Venue, *, fail_first_lookup: bool = False) -> None:
        super().__init__(venue)
        self.fail_first_lookup = fail_first_lookup
        self.lookups = 0

    async def find_by_client_order_id(
        self,
        client_order_id: str,
    ) -> OrderSubmissionResult:
        self.lookups += 1
        if self.fail_first_lookup:
            self.fail_first_lookup = False
            raise RuntimeError("reconciliation endpoint unavailable")
        return OrderSubmissionResult(
            client_order_id,
            OrderStatus.FILLED,
            (
                FillReport(
                    f"{self.venue.value}-recovered-fill",
                    Decimal(10),
                    Decimal("0.50"),
                    Decimal(0),
                ),
            ),
        )


class AmbiguousTradingPort(FakeTradingPort):
    async def submit_fok(self, request: OrderRequest) -> OrderSubmissionResult:
        self.submissions += 1
        raise OrderOutcomeUnknown("response timed out")


class RecordingExecutionSupervisor:
    def __init__(self) -> None:
        self.bound_ports = None
        self.evidence: ExecutionEvidence | None = None
        self.maximum_unhedged_loss: Decimal | None = None

    def bind_emergency_ports(self, ports) -> None:
        self.bound_ports = ports

    async def finalize(
        self,
        _record: ExecutionRecord,
        evidence: ExecutionEvidence | None = None,
        *,
        now: datetime,
        maximum_unhedged_loss: Decimal = Decimal(0),
        submission_permission=None,
    ) -> None:
        assert now == NOW
        self.evidence = evidence
        self.maximum_unhedged_loss = maximum_unhedged_loss


def fee_engine() -> FeeEngine:
    engine = FeeEngine()
    engine.register(
        Venue.KALSHI, "standard", ProbabilityCurveFeeRule("kalshi-v1", Decimal("0.07"))
    )
    engine.register(
        Venue.POLYMARKET, "standard", ProbabilityCurveFeeRule("poly-v1", Decimal(0))
    )
    return engine


class StaleMarketData:
    async def get_books(self, _pair, now: datetime):
        stale = now - timedelta(seconds=5)
        return (
            NormalizedBook(
                "K-MARKET",
                "no",
                "k-seq-stale",
                stale,
                stale,
                (BookLevel(Decimal("0.70"), Decimal(20)),),
            ),
            NormalizedBook(
                "P-TOKEN",
                "yes",
                "p-seq-stale",
                stale,
                stale,
                (BookLevel(Decimal("0.20"), Decimal(20)),),
            ),
        )


class FailingOpportunityStore(InMemoryOpportunityStore):
    def replace(self, records: list[OpportunityRecord]) -> None:
        raise RuntimeError("opportunity publication failed")


class ClosingOpportunityStore(InMemoryOpportunityStore):
    def __init__(self, control: SystemControl) -> None:
        super().__init__()
        self._control = control

    def replace(self, records: list[OpportunityRecord]) -> None:
        super().replace(records)
        self._control.disable_opening("closed after evaluation")


class RefreshingRiskPolicies:
    """Represents a runner retaining an old cache while another API updates it."""

    def __init__(self, stale, refreshed) -> None:
        self.current = stale
        self._refreshed = refreshed
        self.refreshes = 0

    async def refresh(self):
        self.refreshes += 1
        return self._refreshed


class FailOnceSettledSaveStore(InMemoryExecutionStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_settled_save = True
        self.list_calls = 0

    async def list(self) -> list[ExecutionRecord]:
        self.list_calls += 1
        return await super().list()

    async def save(self, record: ExecutionRecord) -> None:
        if record.capital_settled and self.fail_settled_save:
            self.fail_settled_save = False
            raise RuntimeError("capital settlement persistence unavailable")
        await super().save(record)


class CancelAfterReserveLedger(CapitalLedger):
    async def reserve_pair(
        self,
        correlation_id: str,
        kalshi_amount: Decimal,
        polymarket_amount: Decimal,
        *,
        event_id: str | None = None,
    ):
        await super().reserve_pair(
            correlation_id,
            kalshi_amount,
            polymarket_amount,
            event_id=event_id,
        )
        raise asyncio.CancelledError


async def configured_integrations() -> IntegrationConfigService:
    service = IntegrationConfigService(
        InMemoryIntegrationConfigRepository(),
        InMemorySecretStore(),
    )
    values = {
        IntegrationProvider.ODDPOOL: (
            ODDPOOL_BASE_URL,
            {},
            {"api_token": "token"},
        ),
        IntegrationProvider.KALSHI: (
            "https://kalshi.test",
            {"key_id": "key"},
            {"private_key": "pem"},
        ),
        IntegrationProvider.POLYMARKET: (
            "https://clob.test",
            {
                "account_type": "eoa",
                "wallet_address": OWNER_ADDRESS,
                "funder_address": OWNER_ADDRESS,
                "signature_type": "0",
                "chain_id": 137,
            },
            {"private_key": PRIVATE_KEY},
        ),
    }
    for provider, (base_url, configuration, secrets) in values.items():
        await service.update(
            provider,
            enabled=True,
            environment=IntegrationEnvironment.PRODUCTION,
            base_url=base_url,
            configuration=configuration,
            secrets=secrets,
            actor="test",
        )
    return service


@pytest.mark.asyncio
async def test_live_cycle_does_not_reserve_or_submit_when_publication_fails() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Publication failure pair",
            kalshi_market_id="K-PUBLISH",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-PUBLISH",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    control = SystemControl(opening_enabled=True)
    history = InMemoryExecutionStore()
    ports = {
        Venue.KALSHI: FakeTradingPort(Venue.KALSHI),
        Venue.POLYMARKET: FakeTradingPort(Venue.POLYMARKET),
    }
    capital = CapitalLedger({})
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=InMemoryRiskPolicyStore(RiskPolicyInput.defaults()),
        system_control=control,
        execution_store=history,
        opportunities=FailingOpportunityStore(),
        runtime_status=RuntimeStatusService(integrations, control),
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
    )

    with pytest.raises(RuntimeError, match="opportunity publication failed"):
        await runtime.run_once(NOW)

    assert await history.list() == []
    assert capital.reservations == {}
    assert all(port.submissions == 0 for port in ports.values())


@pytest.mark.asyncio
async def test_closing_opening_after_evaluation_publishes_without_submitting() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Closing control pair",
            kalshi_market_id="K-CLOSING",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-CLOSING",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    control = SystemControl(opening_enabled=True)
    history = InMemoryExecutionStore()
    ports = {
        Venue.KALSHI: FakeTradingPort(Venue.KALSHI),
        Venue.POLYMARKET: FakeTradingPort(Venue.POLYMARKET),
    }
    capital = CapitalLedger({})
    opportunities = ClosingOpportunityStore(control)
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=InMemoryRiskPolicyStore(RiskPolicyInput.defaults()),
        system_control=control,
        execution_store=history,
        opportunities=opportunities,
        runtime_status=RuntimeStatusService(integrations, control),
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
    )

    executions = await runtime.run_once(NOW)

    [published] = opportunities.list_ranked()
    assert executions == 0
    assert published.rejection_reasons == ()
    assert await history.list() == []
    assert capital.reservations == {}
    assert all(port.submissions == 0 for port in ports.values())


@pytest.mark.asyncio
async def test_live_cycle_executes_reviewed_profitable_pair_once_per_book_sequence() -> (
    None
):
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Executable pair",
            kalshi_market_id="K-MARKET",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-TOKEN",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    risk = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())
    control = SystemControl(opening_enabled=False)
    history = InMemoryExecutionStore()
    opportunities = InMemoryOpportunityStore()
    status = RuntimeStatusService(integrations, control)
    kalshi_port = FakeTradingPort(Venue.KALSHI)
    polymarket_port = FakeTradingPort(Venue.POLYMARKET)
    ports: dict[Venue, BalanceTradingPort] = {
        Venue.KALSHI: kalshi_port,
        Venue.POLYMARKET: polymarket_port,
    }
    capital = CapitalLedger({})
    supervisor = RecordingExecutionSupervisor()
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=risk,
        system_control=control,
        execution_store=history,
        opportunities=opportunities,
        runtime_status=status,
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
        execution_supervisor=supervisor,
    )

    first = await runtime.run_once(NOW)
    [published] = opportunities.list_ranked()
    records_before_opening = await history.list()
    submissions_before_opening = [kalshi_port.submissions, polymarket_port.submissions]
    reservations_before_opening = dict(capital.reservations)
    control.set_opening(True, "operator enabled real ordering")
    second = await runtime.run_once(NOW)
    restarted_runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=risk,
        system_control=control,
        execution_store=history,
        opportunities=InMemoryOpportunityStore(),
        runtime_status=status,
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
    )
    after_restart = await restarted_runtime.run_once(NOW)

    records = await history.list()
    assert first == 0
    assert published.rejection_reasons == ()
    assert records_before_opening == []
    assert submissions_before_opening == [0, 0]
    assert reservations_before_opening == {}
    assert second == 1
    assert after_restart == 0
    assert len(records) == 1
    assert records[0].state is ExecutionState.PAIRED
    assert kalshi_port.submissions == 1
    assert polymarket_port.submissions == 1
    assert len(capital.consumed_pairs) == 1
    assert capital.reservations == {}
    assert status.executions_started == 1
    assert supervisor.bound_ports is ports
    assert supervisor.evidence is not None
    assert supervisor.evidence.estimated_fees[0] > 0
    assert supervisor.maximum_unhedged_loss == risk.current.maximum_unhedged_loss


@pytest.mark.asyncio
async def test_cancel_after_reservation_commit_releases_unclaimed_capital() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Cancelled reservation pair",
            kalshi_market_id="K-MARKET",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-TOKEN",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    control = SystemControl(opening_enabled=True)
    history = InMemoryExecutionStore()
    ports = {
        Venue.KALSHI: FakeTradingPort(Venue.KALSHI),
        Venue.POLYMARKET: FakeTradingPort(Venue.POLYMARKET),
    }
    capital = CancelAfterReserveLedger({})
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=InMemoryRiskPolicyStore(RiskPolicyInput.defaults()),
        system_control=control,
        execution_store=history,
        opportunities=InMemoryOpportunityStore(),
        runtime_status=RuntimeStatusService(integrations, control),
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime.run_once(NOW)

    assert capital.reservations == {}
    assert await history.list() == []
    assert all(port.submissions == 0 for port in ports.values())


@pytest.mark.asyncio
async def test_concurrent_live_cycles_submit_each_book_once() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Concurrent pair",
            kalshi_market_id="K-CONCURRENT",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-CONCURRENT",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    control = SystemControl(opening_enabled=True)
    history = InMemoryExecutionStore()
    ports = {
        Venue.KALSHI: YieldingTradingPort(Venue.KALSHI),
        Venue.POLYMARKET: YieldingTradingPort(Venue.POLYMARKET),
    }
    capital = CapitalLedger({})
    market_data = ConcurrentMarketData()
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=InMemoryRiskPolicyStore(RiskPolicyInput.defaults()),
        system_control=control,
        execution_store=history,
        opportunities=InMemoryOpportunityStore(),
        runtime_status=RuntimeStatusService(integrations, control),
        market_data_factory=lambda _bundle: market_data,
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
    )

    first, second = await asyncio.gather(runtime.run_once(NOW), runtime.run_once(NOW))

    records = await history.list()
    assert sorted((first, second)) == [0, 1]
    assert len(records) == 1
    assert records[0].state is ExecutionState.PAIRED
    assert all(port.submissions == 1 for port in ports.values())
    assert len(capital.consumed_pairs) == 1
    assert capital.reservations == {}
    assert market_data.maximum_active_calls == 1


@pytest.mark.asyncio
async def test_live_cycle_uses_the_risk_policy_snapshot_refreshed_this_cycle() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Fresh policy pair",
            kalshi_market_id="K-FRESH-POLICY",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-FRESH-POLICY",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    source = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())
    stale = source.current
    updated_input = RiskPolicyInput.defaults()
    updated_input.minimum_roi = Decimal("0.99")
    refreshed = await source.create(updated_input)
    risk = RefreshingRiskPolicies(stale, refreshed)
    control = SystemControl(opening_enabled=False)
    opportunities = InMemoryOpportunityStore()
    ports = {
        Venue.KALSHI: FakeTradingPort(Venue.KALSHI),
        Venue.POLYMARKET: FakeTradingPort(Venue.POLYMARKET),
    }
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=risk,
        system_control=control,
        execution_store=InMemoryExecutionStore(),
        opportunities=opportunities,
        runtime_status=RuntimeStatusService(integrations, control),
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=CapitalLedger({}),
    )

    await runtime.run_once(NOW)

    [opportunity] = opportunities.list_ranked()
    assert risk.refreshes == 1
    assert opportunity.risk_policy_version == str(refreshed.version)
    assert opportunity.rejection_reasons == ("ROI_BELOW_THRESHOLD",)
    assert all(port.submissions == 0 for port in ports.values())


@pytest.mark.asyncio
async def test_two_runtimes_leave_the_winners_reservation_on_a_lost_claim() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Cross process claim pair",
            kalshi_market_id="K-CROSS-PROCESS",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-CROSS-PROCESS",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    opening_store = InMemoryOpeningControlStore(
        OpeningControlState(True, "operator enabled", version=1)
    )
    refresh_barrier = asyncio.Barrier(2)

    class ConcurrentControl(SystemControl):
        async def refresh_async(self):
            state = await super().refresh_async()
            await refresh_barrier.wait()
            return state

    first_control = ConcurrentControl(store=opening_store)
    second_control = ConcurrentControl(store=opening_store)
    history = InMemoryExecutionStore()
    capital = CapitalLedger({})
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingTradingPort(FakeTradingPort):
        async def submit_fok(self, request: OrderRequest) -> OrderSubmissionResult:
            started.set()
            await release.wait()
            return await super().submit_fok(request)

    ports = {
        Venue.KALSHI: BlockingTradingPort(Venue.KALSHI),
        Venue.POLYMARKET: BlockingTradingPort(Venue.POLYMARKET),
    }

    def runtime(control: SystemControl) -> LiveRuntimeService:
        return LiveRuntimeService(
            integrations=integrations,
            pairs=pairs,
            risk_policies=InMemoryRiskPolicyStore(RiskPolicyInput.defaults()),
            system_control=control,
            execution_store=history,
            opportunities=InMemoryOpportunityStore(),
            runtime_status=RuntimeStatusService(integrations, control),
            market_data_factory=lambda _bundle: FakeMarketData(),
            trading_ports_factory=lambda _bundle: ports,
            optimizer=QuoteOptimizer(fee_engine()),
            capital_ledger=capital,
        )

    first = asyncio.create_task(runtime(first_control).run_once(NOW))
    second = asyncio.create_task(runtime(second_control).run_once(NOW))
    await started.wait()
    await asyncio.sleep(0)
    correlation_id = str(
        uuid5(NAMESPACE_URL, f"live-execution:{pair.id}:k-seq-1:p-seq-1")
    )

    assert capital.get_pair(correlation_id) is not None
    release.set()
    results = await asyncio.gather(first, second)

    assert sorted(results) == [0, 1]
    assert all(port.submissions == 1 for port in ports.values())
    assert correlation_id in capital.consumed_pairs
    assert capital.reservations == {}


@pytest.mark.asyncio
async def test_off_cycle_retries_submitted_recovery_and_converts_reservation() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Recoverable pair",
            kalshi_market_id="K-RECOVERY",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-RECOVERY",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    correlation_id = str(
        uuid5(NAMESPACE_URL, f"live-execution:{pair.id}:old-k-seq:old-p-seq")
    )
    control = SystemControl(opening_enabled=True)
    history = InMemoryExecutionStore()
    await history.save(
        ExecutionRecord(
            correlation_id=correlation_id,
            state=ExecutionState.SUBMITTED,
            requested_quantity=Decimal(10),
            evidence=ExecutionEvidence(
                quote_evaluation_id="old-quote",
                rule_versions=("old-k-rule", "old-p-rule"),
                book_sequences=("old-k-seq", "old-p-seq"),
                balance_versions=("old-k-balance", "old-p-balance"),
                risk_policy_version="risk-1",
                capital_reservation_id="old-reservation",
                quantity=Decimal(10),
                kalshi_market_id="K-RECOVERY",
                polymarket_market_id="P-RECOVERY",
                kalshi_outcome="no",
                polymarket_outcome="yes",
                kalshi_limit_price=Decimal("0.70"),
                polymarket_limit_price=Decimal("0.20"),
                conservative_roi=Decimal("0.08"),
                minimum_roi=Decimal("0.03"),
            ),
        )
    )
    ports = {
        Venue.KALSHI: RetryableRecoveryPort(Venue.KALSHI, fail_first_lookup=True),
        Venue.POLYMARKET: RetryableRecoveryPort(Venue.POLYMARKET),
    }
    capital = CapitalLedger({})
    capital.sync_available_balances(
        kalshi_available=Decimal(100),
        polymarket_available=Decimal(100),
    )
    await capital.reserve_pair(
        correlation_id,
        Decimal(7),
        Decimal(2),
        event_id=pair.id,
    )
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=InMemoryRiskPolicyStore(RiskPolicyInput.defaults()),
        system_control=control,
        execution_store=history,
        opportunities=InMemoryOpportunityStore(),
        runtime_status=RuntimeStatusService(integrations, control),
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
    )

    with pytest.raises(RuntimeError, match="reconciliation endpoint unavailable"):
        await runtime.run_once(NOW)
    assert (await history.get(correlation_id)).state is ExecutionState.SUBMITTED
    assert capital.get_pair(correlation_id) is not None
    assert all(port.submissions == 0 for port in ports.values())

    control.disable_opening("recover before opening a new position")
    executions = await runtime.run_once(NOW)

    assert executions == 0
    assert (await history.get(correlation_id)).state is ExecutionState.PAIRED
    assert ports[Venue.KALSHI].lookups == 2
    assert ports[Venue.POLYMARKET].lookups == 1
    assert all(port.submissions == 0 for port in ports.values())
    assert correlation_id in capital.consumed_pairs
    assert capital.reservations == {}

    assert await runtime.run_once(NOW) == 0
    assert ports[Venue.KALSHI].lookups == 2
    assert ports[Venue.POLYMARKET].lookups == 1


@pytest.mark.asyncio
async def test_terminal_settlement_failure_retries_without_resubmitting_orders() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Settlement retry pair",
            kalshi_market_id="K-SETTLEMENT",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-SETTLEMENT",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    control = SystemControl(opening_enabled=True)
    history = FailOnceSettledSaveStore()
    ports = {
        Venue.KALSHI: FakeTradingPort(Venue.KALSHI),
        Venue.POLYMARKET: FakeTradingPort(Venue.POLYMARKET),
    }
    capital = CapitalLedger({})
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=InMemoryRiskPolicyStore(RiskPolicyInput.defaults()),
        system_control=control,
        execution_store=history,
        opportunities=InMemoryOpportunityStore(),
        runtime_status=RuntimeStatusService(integrations, control),
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
    )

    with pytest.raises(RuntimeError, match="capital settlement persistence unavailable"):
        await runtime.run_once(NOW)
    [record] = await history.list()
    assert record.state is ExecutionState.PAIRED
    assert record.capital_settled is False
    assert all(port.submissions == 1 for port in ports.values())
    assert capital.reservations == {}
    assert len(capital.consumed_pairs) == 1
    history.list_calls = 0

    executions = await runtime.run_once(NOW)

    assert executions == 0
    assert all(port.submissions == 1 for port in ports.values())
    assert capital.reservations == {}
    assert len(capital.consumed_pairs) == 1
    assert (await history.get(record.correlation_id)).capital_settled is True
    assert history.list_calls == 0


@pytest.mark.asyncio
async def test_off_cycle_recovers_submitted_execution_without_current_pairs() -> None:
    integrations = await configured_integrations()
    control = SystemControl(opening_enabled=False)
    history = InMemoryExecutionStore()
    correlation_id = "old-submitted-execution"
    await history.save(
        ExecutionRecord(
            correlation_id=correlation_id,
            state=ExecutionState.SUBMITTED,
            requested_quantity=Decimal(10),
        )
    )
    ports = {
        Venue.KALSHI: RetryableRecoveryPort(Venue.KALSHI),
        Venue.POLYMARKET: RetryableRecoveryPort(Venue.POLYMARKET),
    }
    capital = CapitalLedger({})
    capital.sync_available_balances(
        kalshi_available=Decimal(100),
        polymarket_available=Decimal(100),
    )
    await capital.reserve_pair(correlation_id, Decimal(7), Decimal(2))
    opportunities = InMemoryOpportunityStore()
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=ExecutablePairService(InMemoryExecutablePairRepository()),
        risk_policies=InMemoryRiskPolicyStore(RiskPolicyInput.defaults()),
        system_control=control,
        execution_store=history,
        opportunities=opportunities,
        runtime_status=RuntimeStatusService(integrations, control),
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
    )

    executions = await runtime.run_once(NOW)

    assert executions == 0
    assert opportunities.list_ranked() == []
    assert (await history.get(correlation_id)).state is ExecutionState.PAIRED
    assert all(port.submissions == 0 for port in ports.values())
    assert all(port.lookups == 1 for port in ports.values())
    assert correlation_id in capital.consumed_pairs


@pytest.mark.asyncio
async def test_live_cycle_releases_reservation_when_order_outcome_stays_unknown() -> (
    None
):
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Executable pair",
            kalshi_market_id="K-MARKET",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-TOKEN",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    risk = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())
    control = SystemControl(opening_enabled=True)
    history = InMemoryExecutionStore()
    status = RuntimeStatusService(integrations, control)
    ports = {
        Venue.KALSHI: AmbiguousTradingPort(Venue.KALSHI),
        Venue.POLYMARKET: AmbiguousTradingPort(Venue.POLYMARKET),
    }
    capital = CapitalLedger({})
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=risk,
        system_control=control,
        execution_store=history,
        opportunities=InMemoryOpportunityStore(),
        runtime_status=status,
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
    )

    executions = await runtime.run_once(NOW)

    records = await history.list()
    assert executions == 1
    assert records[0].state is ExecutionState.EXCEPTION
    assert capital.reservations == {}
    assert capital.consumed_pairs == {}


@pytest.mark.asyncio
async def test_late_settlement_is_retained_as_structured_rejection() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Late settlement pair",
            kalshi_market_id="K-LATE",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-LATE",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
            worst_case_settlement_at=NOW + timedelta(days=31),
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    risk = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())
    control = SystemControl(opening_enabled=True)
    history = InMemoryExecutionStore()
    opportunities = InMemoryOpportunityStore()
    status = RuntimeStatusService(integrations, control)
    ports: dict[Venue, BalanceTradingPort] = {
        Venue.KALSHI: FakeTradingPort(Venue.KALSHI),
        Venue.POLYMARKET: FakeTradingPort(Venue.POLYMARKET),
    }
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=risk,
        system_control=control,
        execution_store=history,
        opportunities=opportunities,
        runtime_status=status,
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=CapitalLedger({}),
    )

    executions = await runtime.run_once(NOW)

    [record] = opportunities.list_ranked()
    assert executions == 0
    assert record.rejection_reasons == ("SETTLEMENT_TOO_LATE",)
    assert record.quantity is None
    assert record.kalshi_vwap is None
    assert record.polymarket_vwap is None
    assert record.total_fees is None
    assert record.deployed_capital is None
    assert record.payout is None
    assert record.profit_floor is None
    assert record.conservative_roi is None
    assert record.book_age_ms is None
    assert record.risk_policy_version != "unavailable"
    assert all(version.startswith("sha256:") for version in record.rule_versions)


@pytest.mark.asyncio
async def test_stale_book_is_retained_as_structured_rejection() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Stale book pair",
            kalshi_market_id="K-STALE",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-STALE",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(10),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    risk = InMemoryRiskPolicyStore(RiskPolicyInput.defaults())
    control = SystemControl(opening_enabled=False)
    history = InMemoryExecutionStore()
    opportunities = InMemoryOpportunityStore()
    status = RuntimeStatusService(integrations, control)
    kalshi_port = FakeTradingPort(Venue.KALSHI)
    polymarket_port = FakeTradingPort(Venue.POLYMARKET)
    ports: dict[Venue, BalanceTradingPort] = {
        Venue.KALSHI: kalshi_port,
        Venue.POLYMARKET: polymarket_port,
    }
    capital = CapitalLedger({})
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=risk,
        system_control=control,
        execution_store=history,
        opportunities=opportunities,
        runtime_status=status,
        market_data_factory=lambda _bundle: StaleMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
    )

    executions = await runtime.run_once(NOW)

    [record] = opportunities.list_ranked()
    assert executions == 0
    assert record.rejection_reasons == ("STALE_BOOK",)
    assert record.quantity is None
    assert record.kalshi_vwap is None
    assert record.polymarket_vwap is None
    assert record.deployed_capital is None
    assert record.profit_floor is None
    assert record.conservative_roi is None
    assert record.book_age_ms == 5000
    assert record.book_sequences == ("k-seq-stale", "p-seq-stale")
    assert await history.list() == []
    assert capital.reservations == {}
    assert kalshi_port.submissions == 0
    assert polymarket_port.submissions == 0
    runtime_view = await status.view()
    assert runtime_view.ready is True
    assert runtime_view.opening_enabled is False
    assert runtime_view.last_error is None


@pytest.mark.asyncio
async def test_below_minimum_quantity_retains_calculated_quote_metrics() -> None:
    integrations = await configured_integrations()
    pairs = ExecutablePairService(InMemoryExecutablePairRepository())
    pair = await pairs.create(
        ExecutablePairInput(
            title="Below minimum quantity pair",
            kalshi_market_id="K-MIN",
            kalshi_outcome="no",
            kalshi_rule_text="K rule",
            kalshi_rule_url="https://kalshi.test/rule",
            polymarket_market_id="P-MIN",
            polymarket_outcome="yes",
            polymarket_rule_text="P rule",
            polymarket_rule_url="https://poly.test/rule",
            minimum_quantity=Decimal(21),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_category="standard",
            polymarket_category="standard",
        )
    )
    await pairs.review(
        pair.id,
        status=MappingStatus.EXACT,
        checklist={item: True for item in REQUIRED_REVIEW_ITEMS},
        truth_table=[{"kalshi": Decimal(1), "polymarket": Decimal(0)}],
        notes="exact",
        reviewer="human",
    )
    risk = InMemoryRiskPolicyStore(
        replace(
            RiskPolicyInput.defaults(),
            per_trade_limit=Decimal(100),
            per_event_limit=Decimal(100),
            portfolio_limit=Decimal(100),
        )
    )
    control = SystemControl(opening_enabled=False)
    history = InMemoryExecutionStore()
    opportunities = InMemoryOpportunityStore()
    status = RuntimeStatusService(integrations, control)
    kalshi_port = FakeTradingPort(Venue.KALSHI)
    polymarket_port = FakeTradingPort(Venue.POLYMARKET)
    ports: dict[Venue, BalanceTradingPort] = {
        Venue.KALSHI: kalshi_port,
        Venue.POLYMARKET: polymarket_port,
    }
    runtime = LiveRuntimeService(
        integrations=integrations,
        pairs=pairs,
        risk_policies=risk,
        system_control=control,
        execution_store=history,
        opportunities=opportunities,
        runtime_status=status,
        market_data_factory=lambda _bundle: FakeMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=CapitalLedger({}),
    )

    executions = await runtime.run_once(NOW)

    [record] = opportunities.list_ranked()
    assert executions == 0
    assert record.rejection_reasons == ("BELOW_MINIMUM_QUANTITY",)
    assert record.quantity == Decimal(20)
    assert record.kalshi_vwap == Decimal("0.70")
    assert record.polymarket_vwap == Decimal("0.20")
    assert record.deployed_capital is not None
    assert record.profit_floor is not None
    assert record.conservative_roi is not None
    assert record.book_age_ms == 0
    assert await history.list() == []
    assert kalshi_port.submissions == 0
    assert polymarket_port.submissions == 0
