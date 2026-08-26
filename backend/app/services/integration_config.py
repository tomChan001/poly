from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from backend.app.adapters.polymarket.account import PolymarketAccountResolver
from backend.app.core.secrets import SecretStore


class IntegrationProvider(StrEnum):
    ODDPOOL = "oddpool"
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class IntegrationEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class RuntimeConfigurationError(ValueError):
    def __init__(self, missing: tuple[str, ...]) -> None:
        super().__init__(f"runtime integrations are not ready: {', '.join(missing)}")
        self.missing = missing


ALLOWED_SECRET_FIELDS: dict[IntegrationProvider, frozenset[str]] = {
    IntegrationProvider.ODDPOOL: frozenset({"api_token"}),
    IntegrationProvider.KALSHI: frozenset({"private_key"}),
    IntegrationProvider.POLYMARKET: frozenset({"private_key", "api_secret", "passphrase"}),
}

ALLOWED_CONFIGURATION_FIELDS: dict[IntegrationProvider, frozenset[str]] = {
    IntegrationProvider.ODDPOOL: frozenset(),
    IntegrationProvider.KALSHI: frozenset({"key_id"}),
    IntegrationProvider.POLYMARKET: frozenset(
        {
            "account_type",
            "owner_address",
            "wallet_address",
            "funder_address",
            "signature_type",
            "chain_id",
            "api_key",
        }
    ),
}

REQUIRED_SECRET_FIELDS: dict[IntegrationProvider, frozenset[str]] = {
    IntegrationProvider.ODDPOOL: frozenset({"api_token"}),
    IntegrationProvider.KALSHI: frozenset({"private_key"}),
    IntegrationProvider.POLYMARKET: frozenset({"private_key"}),
}

REQUIRED_CONFIGURATION_FIELDS: dict[IntegrationProvider, frozenset[str]] = {
    IntegrationProvider.ODDPOOL: frozenset(),
    IntegrationProvider.KALSHI: frozenset({"key_id"}),
    IntegrationProvider.POLYMARKET: frozenset({"account_type", "funder_address", "signature_type", "chain_id"}),
}


@dataclass(frozen=True, slots=True)
class SecretStatus:
    configured: bool
    fingerprint: str | None


@dataclass(frozen=True, slots=True)
class IntegrationConfigRecord:
    provider: IntegrationProvider
    enabled: bool
    environment: IntegrationEnvironment
    base_url: str
    configuration: dict[str, str | int | bool] = field(default_factory=dict)
    version: int = 1
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_by: str = "system"


@dataclass(frozen=True, slots=True)
class IntegrationConfigView:
    provider: IntegrationProvider
    enabled: bool
    environment: IntegrationEnvironment
    base_url: str
    configuration: dict[str, str | int | bool]
    version: int
    updated_at: datetime
    updated_by: str
    secret_status: dict[str, SecretStatus]


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    provider: IntegrationProvider
    ok: bool
    detail: str
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class RuntimeIntegration:
    """Internal configuration boundary; credentials must never enter API views."""

    record: IntegrationConfigRecord
    credentials: dict[str, str]


@dataclass(frozen=True, slots=True)
class RuntimeBundle:
    oddpool: RuntimeIntegration
    kalshi: RuntimeIntegration
    polymarket: RuntimeIntegration


@dataclass(frozen=True, slots=True)
class RuntimeReadiness:
    ready: bool
    missing: tuple[str, ...]


class IntegrationConnectionProbe(Protocol):
    async def test(
        self,
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> ConnectionTestResult: ...


class InMemoryConnectionProbe:
    async def test(
        self,
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> ConnectionTestResult:
        return ConnectionTestResult(record.provider, True, "test connection succeeded")


class IntegrationConfigRepository(Protocol):
    async def list(self) -> list[IntegrationConfigRecord]: ...

    async def get(self, provider: IntegrationProvider) -> IntegrationConfigRecord | None: ...

    async def upsert(self, record: IntegrationConfigRecord) -> IntegrationConfigRecord: ...


class InMemoryIntegrationConfigRepository:
    def __init__(self) -> None:
        self._records: dict[IntegrationProvider, IntegrationConfigRecord] = {}

    async def list(self) -> list[IntegrationConfigRecord]:
        return sorted(self._records.values(), key=lambda item: item.provider.value)

    async def get(self, provider: IntegrationProvider) -> IntegrationConfigRecord | None:
        return self._records.get(provider)

    async def upsert(self, record: IntegrationConfigRecord) -> IntegrationConfigRecord:
        current = self._records.get(record.provider)
        saved = replace(record, version=1 if current is None else current.version + 1)
        self._records[record.provider] = saved
        return saved


class IntegrationConfigService:
    def __init__(
        self,
        repository: IntegrationConfigRepository,
        secrets: SecretStore,
        probe: IntegrationConnectionProbe | None = None,
        polymarket_account_resolver: PolymarketAccountResolver | None = None,
    ) -> None:
        self._repository = repository
        self._secrets = secrets
        self._probe = probe or InMemoryConnectionProbe()
        self._polymarket_account_resolver = polymarket_account_resolver or PolymarketAccountResolver()

    async def list(self) -> list[IntegrationConfigView]:
        return [await self._view(record) for record in await self._repository.list()]

    async def runtime_bundle(self) -> RuntimeBundle:
        integrations: dict[IntegrationProvider, RuntimeIntegration] = {}
        missing: list[str] = []
        for provider in IntegrationProvider:
            record = await self._repository.get(provider)
            credentials = await self._credentials(provider)
            if record is None or not record.enabled:
                missing.append(provider.value)
                continue
            try:
                self._validate_required_fields(provider, record.configuration, credentials)
            except ValueError:
                missing.append(provider.value)
                continue
            integrations[provider] = RuntimeIntegration(record, credentials)

        if missing:
            raise RuntimeConfigurationError(tuple(missing))
        return RuntimeBundle(
            oddpool=integrations[IntegrationProvider.ODDPOOL],
            kalshi=integrations[IntegrationProvider.KALSHI],
            polymarket=integrations[IntegrationProvider.POLYMARKET],
        )

    async def readiness(self) -> RuntimeReadiness:
        try:
            await self.runtime_bundle()
        except RuntimeConfigurationError as exc:
            return RuntimeReadiness(False, exc.missing)
        return RuntimeReadiness(True, ())

    async def update(
        self,
        provider: IntegrationProvider,
        *,
        enabled: bool,
        environment: IntegrationEnvironment,
        base_url: str,
        configuration: dict[str, str | int | bool],
        secrets: dict[str, str | None],
        actor: str,
    ) -> IntegrationConfigView:
        self._validate_fields(provider, configuration, secrets)
        normalized_configuration = dict(configuration)
        merged_credentials = await self._merged_credentials(provider, secrets)
        if provider is IntegrationProvider.POLYMARKET:
            normalized_configuration = await self._normalize_polymarket_configuration(
                normalized_configuration,
                merged_credentials,
            )
            self._validate_polymarket_api_credentials(normalized_configuration, merged_credentials)

        if enabled:
            self._validate_required_fields(provider, normalized_configuration, merged_credentials)

        for name, value in secrets.items():
            if value:
                await self._secrets.set(self._secret_key(provider, name), value)

        saved = await self._repository.upsert(
            IntegrationConfigRecord(
                provider=provider,
                enabled=enabled,
                environment=environment,
                base_url=base_url,
                configuration=normalized_configuration,
                updated_at=datetime.now(UTC),
                updated_by=actor,
            )
        )
        return await self._view(saved)

    async def test_connection(
        self,
        provider: IntegrationProvider,
    ) -> ConnectionTestResult:
        record = await self._repository.get(provider)
        if record is None:
            raise ValueError(f"{provider.value} integration is not configured")
        credentials = await self._credentials(provider)
        self._validate_required_fields(provider, record.configuration, credentials)
        return await self._probe.test(record, credentials)

    async def delete_secret(
        self,
        provider: IntegrationProvider,
        name: str,
    ) -> SecretStatus:
        if name not in ALLOWED_SECRET_FIELDS[provider]:
            raise ValueError(f"unsupported secret field for {provider.value}: {name}")
        await self._secrets.delete(self._secret_key(provider, name))
        return SecretStatus(False, None)

    async def _view(self, record: IntegrationConfigRecord) -> IntegrationConfigView:
        statuses: dict[str, SecretStatus] = {}
        for name in sorted(ALLOWED_SECRET_FIELDS[record.provider]):
            value = await self._secrets.get(self._secret_key(record.provider, name))
            statuses[name] = SecretStatus(
                configured=value is not None,
                fingerprint=self._fingerprint(value) if value is not None else None,
            )
        return IntegrationConfigView(
            provider=record.provider,
            enabled=record.enabled,
            environment=record.environment,
            base_url=record.base_url,
            configuration=dict(record.configuration),
            version=record.version,
            updated_at=record.updated_at,
            updated_by=record.updated_by,
            secret_status=statuses,
        )

    @staticmethod
    def _validate_fields(
        provider: IntegrationProvider,
        configuration: dict[str, str | int | bool],
        secrets: dict[str, str | None],
    ) -> None:
        unsupported_config = set(configuration) - ALLOWED_CONFIGURATION_FIELDS[provider]
        unsupported_secrets = set(secrets) - ALLOWED_SECRET_FIELDS[provider]
        if unsupported_config:
            raise ValueError(
                f"unsupported configuration fields for {provider.value}: "
                f"{sorted(unsupported_config)}"
            )
        if unsupported_secrets:
            raise ValueError(
                f"unsupported secret fields for {provider.value}: {sorted(unsupported_secrets)}"
            )

    def _validate_required_fields(
        self,
        provider: IntegrationProvider,
        configuration: dict[str, str | int | bool],
        credentials: dict[str, str],
    ) -> None:
        missing_configuration = REQUIRED_CONFIGURATION_FIELDS[provider] - set(configuration)
        missing_secrets = REQUIRED_SECRET_FIELDS[provider] - set(credentials)
        if provider is IntegrationProvider.POLYMARKET:
            missing_configuration |= self._missing_polymarket_api_fields(configuration, credentials)
        if missing_configuration or missing_secrets:
            missing = sorted(missing_configuration | missing_secrets)
            raise ValueError(f"{provider.value} requires configured fields: {missing}")

    async def _credentials(self, provider: IntegrationProvider) -> dict[str, str]:
        credentials: dict[str, str] = {}
        for name in ALLOWED_SECRET_FIELDS[provider]:
            value = await self._secrets.get(self._secret_key(provider, name))
            if value is not None:
                credentials[name] = value
        return credentials

    @staticmethod
    def _secret_key(provider: IntegrationProvider, name: str) -> str:
        return f"{provider.value}:{name}"

    @staticmethod
    def _fingerprint(value: str) -> str:
        return f"sha256:{sha256(value.encode('utf-8')).hexdigest()[:12]}"

    async def _merged_credentials(
        self,
        provider: IntegrationProvider,
        secrets: dict[str, str | None],
    ) -> dict[str, str]:
        credentials = await self._credentials(provider)
        for name, value in secrets.items():
            if value:
                credentials[name] = value
        return credentials

    async def _normalize_polymarket_configuration(
        self,
        configuration: dict[str, str | int | bool],
        credentials: dict[str, str],
    ) -> dict[str, str | int | bool]:
        normalized = dict(configuration)
        normalized_owner = normalized.get("owner_address") or normalized.get("wallet_address")
        private_key = credentials.get("private_key")
        account_type = normalized.get("account_type")
        chain_id = normalized.get("chain_id")
        if private_key is None or account_type is None or chain_id is None:
            normalized.pop("wallet_address", None)
            return normalized
        profile = await self._polymarket_account_resolver.resolve(
            account_type=str(account_type),
            private_key=private_key,
            owner_address=str(normalized_owner) if normalized_owner is not None else None,
            funder_address=str(normalized["funder_address"]) if "funder_address" in normalized else None,
            signature_type=normalized.get("signature_type"),
            chain_id=chain_id,
        )
        normalized.pop("wallet_address", None)
        normalized["account_type"] = profile.account_type.value
        normalized["owner_address"] = profile.owner_address
        normalized["funder_address"] = profile.funder_address
        normalized["signature_type"] = profile.signature_type
        normalized["chain_id"] = profile.chain_id
        return normalized

    @staticmethod
    def _validate_polymarket_api_credentials(
        configuration: dict[str, str | int | bool],
        credentials: dict[str, str],
    ) -> None:
        missing = IntegrationConfigService._missing_polymarket_api_fields(configuration, credentials)
        if missing:
            raise ValueError(
                "polymarket api credentials must include api_key, api_secret, and passphrase together"
            )

    @staticmethod
    def _missing_polymarket_api_fields(
        configuration: dict[str, str | int | bool],
        credentials: dict[str, str],
    ) -> set[str]:
        values = {
            "api_key": configuration.get("api_key"),
            "api_secret": credentials.get("api_secret"),
            "passphrase": credentials.get("passphrase"),
        }
        present = {name for name, value in values.items() if value}
        if not present or len(present) == len(values):
            return set()
        return {name for name in values if not values[name]}
