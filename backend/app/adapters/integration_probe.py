import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Protocol, cast
from urllib.parse import urljoin

import httpx

from backend.app.adapters.kalshi.http_transport import KalshiHttpTransport
from backend.app.adapters.polymarket.sdk_transport import PolymarketSdkTransport
from backend.app.services.integration_config import (
    ConnectionTestResult,
    IntegrationConfigRecord,
    IntegrationProvider,
)


class AccountBalanceTransport(Protocol):
    async def get_available_balance(self) -> Decimal: ...


class PolymarketReadOnlyTransport(Protocol):
    async def probe_read_only(self) -> None: ...


type AccountTransportFactory = Callable[
    [IntegrationConfigRecord, dict[str, str]],
    Awaitable[AccountBalanceTransport],
]
type PolymarketTransportFactory = Callable[
    [IntegrationConfigRecord, dict[str, str]],
    Awaitable[PolymarketReadOnlyTransport],
]


class HttpIntegrationConnectionProbe:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        kalshi_factory: AccountTransportFactory | None = None,
        polymarket_factory: PolymarketTransportFactory | None = None,
    ) -> None:
        self._http = http_client
        self._kalshi_factory = kalshi_factory or self._create_kalshi_transport
        self._polymarket_factory = polymarket_factory or self._create_polymarket_transport

    async def test(
        self,
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> ConnectionTestResult:
        if record.provider is not IntegrationProvider.ODDPOOL:
            return await self._test_authenticated_account(record, secrets)

        url, headers, scope = self._request(record, secrets)
        try:
            response = await self._http.get(url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Only the status is returned. Response bodies from authentication
            # endpoints can echo account metadata and must not reach the UI.
            return ConnectionTestResult(
                record.provider,
                False,
                "ODDPOOL_HTTP_ERROR",
                f"{scope} failed with HTTP {exc.response.status_code}",
            )
        except httpx.HTTPError:
            return ConnectionTestResult(
                record.provider,
                False,
                "ODDPOOL_UNREACHABLE",
                f"{scope} is unreachable",
            )
        return ConnectionTestResult(
            record.provider,
            True,
            "ODDPOOL_CONNECTION_OK",
            f"{scope} succeeded",
        )

    async def _test_authenticated_account(
        self,
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> ConnectionTestResult:
        if record.provider is IntegrationProvider.KALSHI:
            return await self._test_kalshi_account(record, secrets)
        return await self._test_polymarket_account(record, secrets)

    async def _test_kalshi_account(
        self,
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> ConnectionTestResult:
        try:
            transport = await self._kalshi_factory(record, secrets)
            await transport.get_available_balance()
        except Exception:  # noqa: BLE001 - probes return sanitized failures for any SDK error
            return ConnectionTestResult(
                record.provider,
                False,
                "KALSHI_BALANCE_ACCESS_FAILED",
                "authenticated Kalshi balance access failed",
            )
        return ConnectionTestResult(
            record.provider,
            True,
            "KALSHI_BALANCE_ACCESS_OK",
            "authenticated Kalshi balance access succeeded",
        )

    async def _test_polymarket_account(
        self,
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> ConnectionTestResult:
        try:
            transport = await self._polymarket_factory(record, secrets)
            await transport.probe_read_only()
        except Exception:  # noqa: BLE001 - probes return sanitized failures for any SDK error
            return ConnectionTestResult(
                record.provider,
                False,
                "POLYMARKET_READ_ONLY_FAILED",
                "Polymarket read-only authentication failed",
            )
        return ConnectionTestResult(
            record.provider,
            True,
            "POLYMARKET_READ_ONLY_OK",
            "Polymarket read-only authentication succeeded",
        )

    async def _create_kalshi_transport(
        self,
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> AccountBalanceTransport:
        key_id = record.configuration.get("key_id")
        if not isinstance(key_id, str):
            raise TypeError("Kalshi key_id is missing")
        return KalshiHttpTransport(
            record.base_url,
            key_id,
            secrets["private_key"],
            self._http,
        )

    @staticmethod
    async def _create_polymarket_transport(
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> PolymarketReadOnlyTransport:
        configuration = record.configuration
        transport = await PolymarketSdkTransport.from_credentials(
            base_url=record.base_url,
            private_key=secrets["private_key"],
            chain_id=int(configuration["chain_id"]),
            signature_type=int(configuration["signature_type"]),
            funder_address=str(configuration["funder_address"]),
            api_key=_optional_text(configuration.get("api_key")),
            api_secret=secrets.get("api_secret"),
            passphrase=secrets.get("passphrase"),
            derive_only=True,
        )
        return _PolymarketSdkReadOnlyProbe(transport)

    @staticmethod
    def _request(
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> tuple[str, dict[str, str], str]:
        base_url = f"{record.base_url.rstrip('/')}/"
        if record.provider is IntegrationProvider.ODDPOOL:
            return (
                urljoin(base_url, "api/opportunities"),
                {"Authorization": f"Bearer {secrets['api_token']}"},
                "authenticated Oddpool request",
            )
        raise ValueError("public probe is only available for Oddpool")


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class _PolymarketSdkReadOnlyClient(Protocol):
    def get_open_orders(self, only_first_page: bool = False) -> list[dict[str, object]]: ...

    def get_trades(
        self,
        params: object = None,
        only_first_page: bool = False,
    ) -> list[dict[str, object]]: ...


class _PolymarketSdkReadOnlyProbe:
    def __init__(self, transport: PolymarketSdkTransport) -> None:
        self._transport = transport
        self._client = cast(_PolymarketSdkReadOnlyClient, transport._client)

    async def probe_read_only(self) -> None:
        await self._transport.get_available_balance()
        await asyncio.to_thread(self._client.get_open_orders, True)
        await asyncio.to_thread(self._client.get_trades, None, True)
