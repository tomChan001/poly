import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from backend.app.adapters.integration_probe import HttpIntegrationConnectionProbe
from backend.app.adapters.kalshi.http_transport import KalshiHttpTransport
from backend.app.adapters.kalshi.trading import KalshiTradingAdapter
from backend.app.adapters.native_market_data import NativeMarketDataClient
from backend.app.adapters.polymarket.sdk_transport import PolymarketSdkTransport
from backend.app.adapters.polymarket.trading import PolymarketTradingAdapter
from backend.app.core.config import settings
from backend.app.core.secrets import InMemorySecretStore, KeyringSecretStore
from backend.app.db.capital import PostgresCapitalLedger
from backend.app.db.executable_pairs import PostgresExecutablePairRepository
from backend.app.db.executions import PostgresExecutionStore
from backend.app.db.incidents import PostgresIncidentStore
from backend.app.db.integration_config import PostgresIntegrationConfigRepository
from backend.app.db.operational_control import PostgresOperationalControlStore
from backend.app.db.outbox import PostgresOutbox
from backend.app.db.risk_policy import PostgresRiskPolicyStore
from backend.app.domain.enums import Venue
from backend.app.services.automation_gate import AutomationEvidence, AutomationGate
from backend.app.services.capital import CapitalLedger
from backend.app.services.executable_pairs import (
    ExecutablePairService,
    InMemoryExecutablePairRepository,
)
from backend.app.services.execution import ExecutionStore, InMemoryExecutionStore
from backend.app.services.execution_supervisor import (
    ExecutionSupervisor,
    IncidentStore,
    InMemoryIncidentStore,
)
from backend.app.services.fees import FeeEngine
from backend.app.services.integration_config import (
    InMemoryIntegrationConfigRepository,
    IntegrationConfigService,
)
from backend.app.services.live_runtime import BalanceTradingPort, LiveRuntimeService
from backend.app.services.mappings import MappingReviewService
from backend.app.services.notifications import (
    InMemoryOutbox,
    NotificationService,
    OutboxStore,
)
from backend.app.services.opportunities import InMemoryOpportunityStore
from backend.app.services.optimizer import QuoteOptimizer
from backend.app.services.pair_discovery import ConfiguredOddpoolPairDiscoveryService
from backend.app.services.rules import InMemoryRuleStore, RuleService
from backend.app.services.runtime_status import RuntimeStatusService
from backend.app.services.settings import (
    InMemoryRiskPolicyStore,
    RiskPolicyInput,
    RiskPolicyStore,
)
from backend.app.services.system_control import SystemControl


class ApplicationContainer:
    """Owns process-local services; durable repositories can replace stores later."""

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._http_client: httpx.AsyncClient | None = None
        self.rule_store = InMemoryRuleStore()
        self.rules = RuleService(self.rule_store)
        self.mappings = MappingReviewService(self.rule_store)
        self.opportunities = InMemoryOpportunityStore()
        self.fees = FeeEngine()
        self.capital_ledger = CapitalLedger({})
        self.risk_policies: RiskPolicyStore = InMemoryRiskPolicyStore(
            RiskPolicyInput.defaults()
        )
        self.outbox: OutboxStore = InMemoryOutbox()
        self.notifications = NotificationService(self.outbox)
        self.incidents: IncidentStore = InMemoryIncidentStore()
        self.system_control = SystemControl(
            opening_enabled=settings.opening_enabled,
            reason="configured default",
        )
        self.execution_supervisor = ExecutionSupervisor(
            system_control=self.system_control,
            incidents=self.incidents,
            notifications=self.notifications,
        )
        self.executions: ExecutionStore = InMemoryExecutionStore()
        self.executable_pairs = ExecutablePairService(InMemoryExecutablePairRepository())
        self.integration_configs = IntegrationConfigService(
            InMemoryIntegrationConfigRepository(),
            InMemorySecretStore(),
        )
        self.runtime_status = RuntimeStatusService(
            self.integration_configs,
            self.system_control,
        )
        self.live_runtime: LiveRuntimeService | None = None
        self.pair_discovery: ConfiguredOddpoolPairDiscoveryService | None = None
        self.automation_gate = AutomationGate()
        self.automation_evidence: AutomationEvidence | None = None

    @classmethod
    def runtime(cls) -> "ApplicationContainer":
        container = cls()
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        container._engine = engine
        container._http_client = http_client
        container.risk_policies = PostgresRiskPolicyStore(sessions)
        container.integration_configs = IntegrationConfigService(
            PostgresIntegrationConfigRepository(
                sessions
            ),
            KeyringSecretStore(settings.credential_service_name),
            HttpIntegrationConnectionProbe(http_client),
        )
        container.executions = PostgresExecutionStore(
            sessions
        )
        container.executable_pairs = ExecutablePairService(
            PostgresExecutablePairRepository(
                sessions
            )
        )
        container.capital_ledger = PostgresCapitalLedger(sessions)
        container.outbox = PostgresOutbox(sessions)
        container.notifications = NotificationService(container.outbox)
        container.incidents = PostgresIncidentStore(sessions)
        container.system_control = SystemControl(
            opening_enabled=settings.opening_enabled,
            reason="configured default",
            store=PostgresOperationalControlStore(sessions),
        )
        container.execution_supervisor = ExecutionSupervisor(
            system_control=container.system_control,
            incidents=container.incidents,
            notifications=container.notifications,
        )
        container.runtime_status = RuntimeStatusService(
            container.integration_configs,
            container.system_control,
        )
        container.pair_discovery = ConfiguredOddpoolPairDiscoveryService(
            container.integration_configs,
            container.executable_pairs,
            http_client,
            polymarket_gamma_url=settings.polymarket_gamma_url,
        )
        ports_cache: dict[Venue, BalanceTradingPort] = {}
        ports_cache_version: tuple[int, int] | None = None

        def market_data_factory(bundle):
            return NativeMarketDataClient(
                kalshi_base_url=bundle.kalshi.record.base_url,
                polymarket_base_url=bundle.polymarket.record.base_url,
                http_client=http_client,
            )

        async def trading_ports_factory(bundle):
            nonlocal ports_cache_version
            # SDK credentials are derived once per process. Reusing the same
            # transports preserves same-process order-ID observations. The CLOB
            # has no client-order-ID query, so an unobserved timeout stays UNKNOWN
            # until durable reconciliation records an exchange identifier.
            # A saved configuration increments its record version, so corrected
            # credentials take effect without requiring an API restart.
            version = (bundle.kalshi.record.version, bundle.polymarket.record.version)
            if ports_cache and ports_cache_version == version:
                return ports_cache
            ports_cache.clear()
            kalshi = KalshiHttpTransport(
                bundle.kalshi.record.base_url,
                str(bundle.kalshi.record.configuration["key_id"]),
                bundle.kalshi.credentials["private_key"],
                http_client,
            )
            poly_config = bundle.polymarket.record.configuration
            polymarket = await PolymarketSdkTransport.from_credentials(
                base_url=bundle.polymarket.record.base_url,
                private_key=bundle.polymarket.credentials["private_key"],
                chain_id=int(poly_config["chain_id"]),
                signature_type=int(poly_config["signature_type"]),
                funder_address=str(poly_config["funder_address"]),
                api_key=_optional_text(poly_config.get("api_key")),
                api_secret=bundle.polymarket.credentials.get("api_secret"),
                passphrase=bundle.polymarket.credentials.get("passphrase"),
            )
            ports_cache.update(
                {
                    Venue.KALSHI: KalshiTradingAdapter(kalshi),
                    Venue.POLYMARKET: PolymarketTradingAdapter(polymarket),
                }
            )
            ports_cache_version = version
            return ports_cache

        container.live_runtime = LiveRuntimeService(
            integrations=container.integration_configs,
            pairs=container.executable_pairs,
            risk_policies=container.risk_policies,
            system_control=container.system_control,
            execution_store=container.executions,
            opportunities=container.opportunities,
            runtime_status=container.runtime_status,
            market_data_factory=market_data_factory,
            trading_ports_factory=trading_ports_factory,
            optimizer=QuoteOptimizer(container.fees),
            capital_ledger=container.capital_ledger,
            execution_supervisor=container.execution_supervisor,
        )
        return container

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
        if self._engine is not None:
            await self._engine.dispose()


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None and str(value) else None
