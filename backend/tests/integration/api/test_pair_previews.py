from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from backend.app.container import ApplicationContainer
from backend.app.services.executable_pairs import ExecutablePairInput
from backend.app.services.pair_previews import PairPreviewService
from backend.tests.integration.api.test_executable_pairs import reviewed_app
from backend.tests.unit.services.test_pair_previews import Books, optimizer, pair


@pytest.mark.asyncio
async def test_pair_preview_api_uses_current_policy_and_preserves_plain_list():
    container = ApplicationContainer()
    value = pair()
    await container.executable_pairs.create(
        ExecutablePairInput(
            **{
                name: getattr(value, name)
                for name in ExecutablePairInput.__dataclass_fields__
            }
        )
    )

    class FixedPreview(PairPreviewService):
        async def evaluate(self, value, policy, now=None):
            return await super().evaluate(value, policy, datetime.now(UTC))

    container.pair_previews = FixedPreview(optimizer(), Books())
    transport = httpx.ASGITransport(app=reviewed_app(container))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ordinary = (await client.get("/api/pairs")).json()
        assert not ordinary[0].get("preview")
        first = (await client.get("/api/pairs?with_preview=true")).json()[0]
        assert first["preview"]["eligible"] is True
        assert isinstance(first["preview"]["conservative_roi"], str)
        policy = container.risk_policies.current
        from backend.app.services.settings import RiskPolicyInput

        updated = RiskPolicyInput(
            **{
                name: getattr(policy, name)
                for name in RiskPolicyInput.__dataclass_fields__
            }
        )
        await container.risk_policies.create(
            replace(updated, minimum_roi=Decimal("0.5"))
        )
        second = (await client.get("/api/pairs?with_preview=true")).json()[0]
        assert second["preview"]["eligible"] is False
        assert "ROI_BELOW_THRESHOLD" in second["preview"]["rejection_reasons"]
        assert (
            first["preview"]["risk_policy_version"]
            != second["preview"]["risk_policy_version"]
        )
        assert second["status"] == "pending_review"


@pytest.mark.asyncio
async def test_configured_preview_uses_public_native_fees_and_books_without_credentials():
    from backend.app.services.integration_config import (
        InMemoryIntegrationConfigRepository,
        IntegrationConfigRecord,
        IntegrationEnvironment,
        IntegrationProvider,
    )
    from backend.tests.unit.adapters.test_native_fees import _pair, _responses

    container = ApplicationContainer()
    public = InMemoryIntegrationConfigRepository()
    for provider, base_url in (
        (IntegrationProvider.KALSHI, "https://kalshi.test"),
        (IntegrationProvider.POLYMARKET, "https://clob.test"),
    ):
        await public.upsert(
            IntegrationConfigRecord(
                provider=provider,
                enabled=True,
                environment=IntegrationEnvironment.PRODUCTION,
                base_url=base_url,
            )
        )
    container._preview_configurations = public
    value = replace(
        _pair(),
        enabled=True,
        kalshi_expected_settlement_at=datetime.now(UTC) + timedelta(days=2),
        polymarket_expected_settlement_at=datetime.now(UTC) + timedelta(days=3),
    )
    await container.executable_pairs.create(
        ExecutablePairInput(
            **{
                name: getattr(value, name)
                for name in ExecutablePairInput.__dataclass_fields__
            }
        )
    )
    requested = []
    responses = _responses()

    def handler(request):
        assert request.method == "GET"
        assert "authorization" not in request.headers
        requested.append(request.url.path)
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(
                200,
                json={
                    "orderbook_fp": {
                        "yes_dollars": [["0.30", "20"]],
                        "no_dollars": [["0.70", "20"]],
                    }
                },
            )
        if request.url.path == "/book":
            return httpx.Response(
                200,
                json={
                    "hash": "book-1",
                    "timestamp": str(int(datetime.now(UTC).timestamp() * 1000)),
                    "asks": [{"price": "0.20", "size": "30"}],
                },
            )
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as native:
        container._http_client = native
        transport = httpx.ASGITransport(app=reviewed_app(container))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            result = await client.get("/api/pairs?with_preview=true")
    assert result.status_code == 200
    preview = result.json()[0]["preview"]
    assert preview["eligible"] is True
    assert Decimal(preview["total_fees"]) > 0
    assert preview["paired_liquidity"] == "20"
    assert set(requested) == set(responses) | {
        "/trade-api/v2/markets/K-MARKET/orderbook",
        "/book",
    }
