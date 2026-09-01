import pytest

from backend.app.adapters.polymarket.account import PolymarketAccountResolver
from backend.app.core.secrets import InMemorySecretStore
from backend.app.services.integration_config import (
    ODDPOOL_BASE_URL,
    InMemoryIntegrationConfigRepository,
    IntegrationConfigRecord,
    IntegrationConfigService,
    IntegrationEnvironment,
    IntegrationProvider,
    RuntimeConfigurationError,
)

PRIVATE_KEY = "0x59c6995e998f97a5a0044966f094538c5f7d2b32a3d47ec9d7c2b4e1f7e3b5d1"
OWNER_ADDRESS = "0xeC165c363b4fB6888BD058c6bB1269c77C9b8E81"
PROXY_ADDRESS = "0x1111111111111111111111111111111111111111"


async def lookup_profile(owner_address: str, chain_id: int) -> dict[str, str]:
    assert owner_address == OWNER_ADDRESS
    assert chain_id == 137
    return {"proxyWallet": PROXY_ADDRESS}


async def configure_provider(
    service: IntegrationConfigService,
    provider: IntegrationProvider,
) -> None:
    values: dict[
        IntegrationProvider,
        tuple[str, dict[str, str | int | bool], dict[str, str | None]],
    ] = {
        IntegrationProvider.ODDPOOL: (
            ODDPOOL_BASE_URL,
            {},
            {"api_token": "oddpool-token"},
        ),
        IntegrationProvider.KALSHI: (
            "https://api.elections.kalshi.com",
            {"key_id": "kalshi-key"},
            {"private_key": "kalshi-private-key"},
        ),
        IntegrationProvider.POLYMARKET: (
            "https://clob.polymarket.com",
            {
                "account_type": "magic_proxy",
                "funder_address": PROXY_ADDRESS,
                "signature_type": 1,
                "chain_id": 137,
            },
            {"private_key": PRIVATE_KEY},
        ),
    }
    base_url, configuration, secrets = values[provider]
    await service.update(
        provider,
        enabled=True,
        environment=IntegrationEnvironment.PRODUCTION,
        base_url=base_url,
        configuration=configuration,
        secrets=secrets,
        actor="local-operator",
    )


@pytest.mark.asyncio
async def test_runtime_bundle_requires_every_enabled_provider() -> None:
    service = IntegrationConfigService(
        InMemoryIntegrationConfigRepository(),
        InMemorySecretStore(),
        polymarket_account_resolver=PolymarketAccountResolver(profile_lookup=lookup_profile),
    )
    await configure_provider(service, IntegrationProvider.ODDPOOL)

    with pytest.raises(RuntimeConfigurationError) as error:
        await service.runtime_bundle()

    assert error.value.missing == ("kalshi", "polymarket")


@pytest.mark.asyncio
async def test_runtime_bundle_keeps_credentials_internal() -> None:
    service = IntegrationConfigService(
        InMemoryIntegrationConfigRepository(),
        InMemorySecretStore(),
        polymarket_account_resolver=PolymarketAccountResolver(profile_lookup=lookup_profile),
    )
    for provider in IntegrationProvider:
        await configure_provider(service, provider)

    bundle = await service.runtime_bundle()
    public = await service.readiness()

    assert bundle.kalshi.credentials["private_key"] == "kalshi-private-key"
    assert bundle.polymarket.record.configuration["account_type"] == "magic_proxy"
    assert bundle.polymarket.record.configuration["owner_address"] == OWNER_ADDRESS
    assert bundle.polymarket.record.configuration["funder_address"] == PROXY_ADDRESS
    assert bundle.polymarket.record.configuration["signature_type"] == 1
    assert bundle.polymarket.record.configuration["chain_id"] == 137
    assert public.ready is True
    assert public.missing == ()
    assert "private" not in repr(public).lower()
    assert "token" not in repr(public).lower()


@pytest.mark.asyncio
async def test_polymarket_update_rejects_partial_api_credentials() -> None:
    service = IntegrationConfigService(
        InMemoryIntegrationConfigRepository(),
        InMemorySecretStore(),
        polymarket_account_resolver=PolymarketAccountResolver(profile_lookup=lookup_profile),
    )

    with pytest.raises(ValueError, match="api_key"):
        await service.update(
            IntegrationProvider.POLYMARKET,
            enabled=True,
            environment=IntegrationEnvironment.PRODUCTION,
            base_url="https://clob.polymarket.com",
            configuration={
                "account_type": "magic_proxy",
                "funder_address": PROXY_ADDRESS,
                "signature_type": 1,
                "chain_id": 137,
                "api_key": "existing-key",
            },
            secrets={"private_key": PRIVATE_KEY},
            actor="local-operator",
        )


@pytest.mark.asyncio
async def test_runtime_bundle_fail_closes_when_repo_contains_partial_polymarket_l2_credentials() -> None:
    repository = InMemoryIntegrationConfigRepository()
    secrets = InMemorySecretStore()
    service = IntegrationConfigService(
        repository,
        secrets,
        polymarket_account_resolver=PolymarketAccountResolver(profile_lookup=lookup_profile),
    )
    await configure_provider(service, IntegrationProvider.ODDPOOL)
    await configure_provider(service, IntegrationProvider.KALSHI)
    await secrets.set("polymarket:private_key", PRIVATE_KEY)
    await secrets.set("polymarket:api_secret", "only-secret")
    await repository.upsert(
        IntegrationConfigRecord(
            provider=IntegrationProvider.POLYMARKET,
            enabled=True,
            environment=IntegrationEnvironment.PRODUCTION,
            base_url="https://clob.polymarket.com",
            configuration={
                "account_type": "magic_proxy",
                "owner_address": OWNER_ADDRESS,
                "funder_address": PROXY_ADDRESS,
                "signature_type": 1,
                "chain_id": 137,
                "api_key": "existing-key",
            },
            updated_by="manual-write",
        )
    )

    with pytest.raises(RuntimeConfigurationError) as error:
        await service.runtime_bundle()

    assert error.value.missing == ("polymarket",)
