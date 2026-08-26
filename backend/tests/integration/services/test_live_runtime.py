from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.core.config import TradingMode
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
    InMemoryIntegrationConfigRepository,
    IntegrationConfigService,
    IntegrationEnvironment,
    IntegrationProvider,
)
from backend.app.services.live_runtime import LiveRuntimeService
from backend.app.services.mappings import REQUIRED_REVIEW_ITEMS
from backend.app.services.opportunities import InMemoryOpportunityStore
from backend.app.services.optimizer import QuoteOptimizer
from backend.app.services.runtime_status import RuntimeStatusService
from backend.app.services.settings import InMemoryRiskPolicyStore, RiskPolicyInput
from backend.app.services.system_control import SystemControl

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
        mode: TradingMode,
        now: datetime,
        maximum_unhedged_loss: Decimal = Decimal(0),
    ) -> None:
        assert mode is TradingMode.LIMITED_AUTO
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


async def configured_integrations() -> IntegrationConfigService:
    service = IntegrationConfigService(
        InMemoryIntegrationConfigRepository(),
        InMemorySecretStore(),
    )
    values = {
        IntegrationProvider.ODDPOOL: (
            "https://oddpool.test",
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
    risk = InMemoryRiskPolicyStore()
    risk.create(RiskPolicyInput.defaults())
    control = SystemControl(opening_enabled=True)
    history = InMemoryExecutionStore()
    status = RuntimeStatusService(integrations, control)
    ports = {
        Venue.KALSHI: FakeTradingPort(Venue.KALSHI),
        Venue.POLYMARKET: FakeTradingPort(Venue.POLYMARKET),
    }
    capital = CapitalLedger({})
    supervisor = RecordingExecutionSupervisor()
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
        trading_mode=TradingMode.LIMITED_AUTO,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
        execution_supervisor=supervisor,
    )

    first = await runtime.run_once(NOW)
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
        trading_mode=TradingMode.LIMITED_AUTO,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=capital,
    )
    after_restart = await restarted_runtime.run_once(NOW)

    records = await history.list()
    assert first == 1
    assert second == 0
    assert after_restart == 0
    assert len(records) == 1
    assert records[0].state is ExecutionState.PAIRED
    assert all(port.submissions == 1 for port in ports.values())
    assert len(capital.consumed_pairs) == 1
    assert capital.reservations == {}
    assert status.executions_started == 1
    assert supervisor.bound_ports is ports
    assert supervisor.evidence is not None
    assert supervisor.evidence.estimated_fees[0] > 0
    assert supervisor.maximum_unhedged_loss == risk.current.maximum_unhedged_loss


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
    risk = InMemoryRiskPolicyStore()
    risk.create(RiskPolicyInput.defaults())
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
        trading_mode=TradingMode.LIMITED_AUTO,
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
    risk = InMemoryRiskPolicyStore()
    risk.create(RiskPolicyInput.defaults())
    control = SystemControl(opening_enabled=True)
    history = InMemoryExecutionStore()
    opportunities = InMemoryOpportunityStore()
    status = RuntimeStatusService(integrations, control)
    ports = {
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
        trading_mode=TradingMode.LIMITED_AUTO,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=CapitalLedger({}),
    )

    executions = await runtime.run_once(NOW)

    [record] = opportunities.list_ranked()
    assert executions == 0
    assert record.rejection_reasons == ("SETTLEMENT_TOO_LATE",)
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
    risk = InMemoryRiskPolicyStore()
    risk.create(RiskPolicyInput.defaults())
    control = SystemControl(opening_enabled=True)
    history = InMemoryExecutionStore()
    opportunities = InMemoryOpportunityStore()
    status = RuntimeStatusService(integrations, control)
    ports = {
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
        market_data_factory=lambda _bundle: StaleMarketData(),
        trading_ports_factory=lambda _bundle: ports,
        trading_mode=TradingMode.LIMITED_AUTO,
        optimizer=QuoteOptimizer(fee_engine()),
        capital_ledger=CapitalLedger({}),
    )

    executions = await runtime.run_once(NOW)

    [record] = opportunities.list_ranked()
    assert executions == 0
    assert record.rejection_reasons == ("STALE_BOOK",)
    assert record.book_sequences == ("k-seq-stale", "p-seq-stale")
