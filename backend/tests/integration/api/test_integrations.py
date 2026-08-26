import httpx
import pytest

from backend.app.adapters.polymarket.account import PolymarketAccountResolver
from backend.app.container import ApplicationContainer
from backend.app.core.config import TradingMode, settings
from backend.app.core.secrets import InMemorySecretStore
from backend.app.core.security import Principal, Role, get_current_principal
from backend.app.main import create_app
from backend.app.services.integration_config import (
    ConnectionTestResult,
    InMemoryIntegrationConfigRepository,
    IntegrationConfigService,
)

PRIVATE_KEY = "0x59c6995e998f97a5a0044966f094538c5f7d2b32a3d47ec9d7c2b4e1f7e3b5d1"
OWNER_ADDRESS = "0xeC165c363b4fB6888BD058c6bB1269c77C9b8E81"
PROXY_ADDRESS = "0x1111111111111111111111111111111111111111"


def integration_payload(token: str = "oddpool-secret-token") -> dict[str, object]:
    return {
        "enabled": True,
        "environment": "sandbox",
        "base_url": "https://api.oddpool.test",
        "configuration": {},
        "secrets": {"api_token": token},
    }


def app_for(role: Role):
    app = create_app(ApplicationContainer())
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "operator-1",
        frozenset({role}),
    )
    return app


def polymarket_payload() -> dict[str, object]:
    return {
        "enabled": True,
        "environment": "production",
        "base_url": "https://clob.polymarket.com",
        "configuration": {
            "account_type": "magic_proxy",
            "chain_id": 137,
        },
        "secrets": {"private_key": PRIVATE_KEY},
    }


class FakePolymarketProbe:
    async def test(self, record, secrets):  # type: ignore[no-untyped-def]
        assert record.provider.value == "polymarket"
        assert secrets["private_key"] == PRIVATE_KEY
        return ConnectionTestResult(
            record.provider,
            True,
            "POLYMARKET_READ_ONLY_OK",
            "Polymarket read-only authentication succeeded",
        )


async def lookup_profile(owner_address: str, chain_id: int) -> dict[str, str]:
    assert owner_address == OWNER_ADDRESS
    assert chain_id == 137
    return {"proxyWallet": PROXY_ADDRESS}


def polymarket_app_for(role: Role):
    container = ApplicationContainer()
    container.integration_configs = IntegrationConfigService(
        InMemoryIntegrationConfigRepository(),
        InMemorySecretStore(),
        FakePolymarketProbe(),
        polymarket_account_resolver=PolymarketAccountResolver(profile_lookup=lookup_profile),
    )
    app = create_app(container)
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        "operator-1",
        frozenset({role}),
    )
    return app


@pytest.mark.asyncio
async def test_operator_can_save_credentials_without_reading_them_back() -> None:
    transport = httpx.ASGITransport(app=app_for(Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        saved = await client.put("/api/integrations/oddpool", json=integration_payload())
        listed = await client.get("/api/integrations")

    assert saved.status_code == 200
    assert "oddpool-secret-token" not in saved.text
    assert saved.json()["secret_status"]["api_token"]["configured"] is True
    assert listed.status_code == 200
    assert "oddpool-secret-token" not in listed.text
    assert listed.json()[0]["provider"] == "oddpool"


@pytest.mark.asyncio
async def test_viewer_cannot_change_integration_configuration() -> None:
    transport = httpx.ASGITransport(app=app_for(Role.VIEWER))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/api/integrations/oddpool", json=integration_payload())

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_operator_can_delete_one_stored_secret() -> None:
    transport = httpx.ASGITransport(app=app_for(Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put("/api/integrations/oddpool", json=integration_payload())
        deleted = await client.delete("/api/integrations/oddpool/secrets/api_token")

    assert deleted.status_code == 200
    assert deleted.json()["configured"] is False
    assert deleted.json()["fingerprint"] is None


@pytest.mark.asyncio
async def test_enabled_integration_requires_its_credentials() -> None:
    payload = integration_payload()
    payload["secrets"] = {}
    transport = httpx.ASGITransport(app=app_for(Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/api/integrations/oddpool", json=payload)

    assert response.status_code == 422
    assert "api_token" in response.json()["detail"]


@pytest.mark.asyncio
async def test_operator_can_test_a_configured_connection_without_secret_echo() -> None:
    transport = httpx.ASGITransport(app=app_for(Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put("/api/integrations/oddpool", json=integration_payload())
        response = await client.post("/api/integrations/oddpool/test")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["provider"] == "oddpool"
    assert "oddpool-secret-token" not in response.text


@pytest.mark.asyncio
async def test_polymarket_magic_proxy_configuration_derives_proxy_fields_without_echo() -> None:
    transport = httpx.ASGITransport(app=polymarket_app_for(Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        saved = await client.put("/api/integrations/polymarket", json=polymarket_payload())
        listed = await client.get("/api/integrations")

    assert saved.status_code == 200
    payload = saved.json()
    assert payload["configuration"]["account_type"] == "magic_proxy"
    assert payload["configuration"]["owner_address"] == OWNER_ADDRESS
    assert payload["configuration"]["proxy_address"] == PROXY_ADDRESS
    assert payload["configuration"]["funder_address"] == PROXY_ADDRESS
    assert payload["configuration"]["signature_type"] == 1
    assert payload["secret_status"]["private_key"]["configured"] is True
    assert PRIVATE_KEY not in saved.text
    assert PRIVATE_KEY not in listed.text


@pytest.mark.asyncio
async def test_polymarket_connection_test_returns_stable_read_only_code() -> None:
    transport = httpx.ASGITransport(app=polymarket_app_for(Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put("/api/integrations/polymarket", json=polymarket_payload())
        response = await client.post("/api/integrations/polymarket/test")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "polymarket",
        "ok": True,
        "code": "POLYMARKET_READ_ONLY_OK",
        "detail": "Polymarket read-only authentication succeeded",
        "checked_at": response.json()["checked_at"],
    }
    assert PRIVATE_KEY not in response.text


@pytest.mark.asyncio
async def test_oidc_is_not_a_supported_integration() -> None:
    transport = httpx.ASGITransport(app=app_for(Role.OPERATOR))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/integrations/oidc",
            json={
                "enabled": False,
                "environment": "production",
                "base_url": "https://login.example.test",
                "configuration": {},
                "secrets": {},
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_explicit_local_setup_allows_loopback_in_real_trading_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    monkeypatch.setattr(settings, "trading_mode", TradingMode.LIMITED_AUTO)
    local_transport = httpx.ASGITransport(
        app=create_app(ApplicationContainer(), allow_local_setup=True),
        client=("127.0.0.1", 41000),
    )
    remote_transport = httpx.ASGITransport(
        app=create_app(ApplicationContainer(), allow_local_setup=True),
        client=("203.0.113.10", 41000),
    )

    async with httpx.AsyncClient(transport=local_transport, base_url="http://test") as client:
        local_response = await client.get("/api/integrations")
    async with httpx.AsyncClient(transport=remote_transport, base_url="http://test") as client:
        remote_response = await client.get("/api/integrations")

    assert local_response.status_code == 200
    assert remote_response.status_code == 403
    assert remote_response.json()["detail"] == "local access only"


@pytest.mark.asyncio
async def test_local_setup_rejects_browser_requests_from_non_local_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "local_setup_enabled", True)
    transport = httpx.ASGITransport(
        app=create_app(ApplicationContainer(), allow_local_setup=True),
        client=("127.0.0.1", 41000),
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        hostile = await client.get(
            "/api/integrations",
            headers={"Origin": "https://evil.example"},
        )
        local = await client.get(
            "/api/integrations",
            headers={"Origin": "http://127.0.0.1:5173"},
        )

    assert hostile.status_code == 403
    assert hostile.json()["detail"] == "local access only"
    assert local.status_code == 200
