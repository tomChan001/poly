import asyncio
import base64
import json
import sys
import threading
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from backend.app.adapters.integration_probe import HttpIntegrationConnectionProbe
from backend.app.adapters.kalshi.http_transport import KalshiHttpTransport
from backend.app.adapters.polymarket.sdk_transport import (
    PolymarketSdkTransport,
    _await_thread_completion,
)


@pytest.mark.asyncio
async def test_thread_write_timeout_waits_for_successful_thread_result() -> None:
    started = threading.Event()
    release = threading.Event()

    def post() -> str:
        started.set()
        release.wait()
        return "posted"

    task = asyncio.create_task(
        asyncio.wait_for(_await_thread_completion(post), timeout=0.01)
    )
    try:
        await asyncio.to_thread(started.wait)
        await asyncio.sleep(0.03)
        assert not task.done()
        release.set()
        assert await task == "posted"
    finally:
        release.set()
        if not task.done():
            await task


@pytest.mark.asyncio
async def test_thread_write_survives_repeated_cancellation_until_release() -> None:
    started = threading.Event()
    release = threading.Event()

    def post() -> str:
        started.set()
        release.wait()
        return "posted"

    task = asyncio.create_task(_await_thread_completion(post))
    try:
        await asyncio.to_thread(started.wait)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        assert await task == "posted"
    finally:
        release.set()
        if not task.done():
            await task
from backend.app.services.integration_config import (
    ConnectionTestResult,
    IntegrationConfigRecord,
    IntegrationEnvironment,
    IntegrationProvider,
)


@pytest.mark.asyncio
async def test_kalshi_transport_signs_account_and_order_requests() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timestamp = request.headers["KALSHI-ACCESS-TIMESTAMP"]
        path = request.url.path
        message = f"{timestamp}{request.method}{path}".encode()
        private_key.public_key().verify(
            base64.b64decode(request.headers["KALSHI-ACCESS-SIGNATURE"]),
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        assert request.headers["KALSHI-ACCESS-KEY"] == "key-id"
        seen_paths.append(path)
        if path.endswith("/balance"):
            return httpx.Response(200, json={"balance": 12345})
        payload = json.loads(request.content)
        assert isinstance(payload, dict)
        return httpx.Response(
            200,
            json={
                "order": {
                    "order_id": "order-1",
                    "client_order_id": payload["client_order_id"],
                    "status": "executed",
                    "fill_count": payload["count"],
                    "average_fill_price": payload["no_price"],
                    "fees": "0.03",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = KalshiHttpTransport(
            "https://api.elections.kalshi.com",
            "key-id",
            private_pem,
            client,
            timestamp_ms=lambda: 1_787_000_000_000,
        )
        balance = await transport.get_available_balance()
        order = await transport.create_order(
            {
                "ticker": "K-MARKET",
                "client_order_id": "corr-kalshi",
                "type": "limit",
                "action": "buy",
                "side": "no",
                "count": 10,
                "no_price": 70,
                "time_in_force": "fill_or_kill",
            },
        )

    assert balance == Decimal("123.45")
    response_order = order["order"]
    assert isinstance(response_order, dict)
    assert response_order["client_order_id"] == "corr-kalshi"
    assert seen_paths == [
        "/trade-api/v2/portfolio/balance",
        "/trade-api/v2/portfolio/orders",
    ]


class FakeClobClient:
    def __init__(self) -> None:
        self.created: object | None = None
        self.posted_type: object | None = None
        self.order_requests: list[str] = []
        self.trade_requests: list[object] = []
        self.open_order_requests = 0

    def create_order(self, order: object) -> object:
        self.created = order
        return {"signed": True}

    def post_order(
        self, order: object, order_type: object, *, defer_exec: bool = False
    ) -> dict[str, object]:
        assert order == {"signed": True}
        self.posted_type = order_type
        return {
            "success": True,
            "orderID": "poly-order-1",
            "status": "matched",
            "tradeIDs": ["trade-1"],
        }

    def get_order(self, order_id: str) -> dict[str, object]:
        self.order_requests.append(order_id)
        return {
            "id": order_id,
            "status": "ORDER_STATUS_MATCHED",
            "size_matched": "9.25",
            "associate_trades": ["trade-1"],
        }

    def get_trades(
        self,
        params: object = None,
        only_first_page: bool = False,
    ) -> list[dict[str, object]]:
        self.trade_requests.append((params, only_first_page))
        return [
            {
                "id": "trade-1",
                "taker_order_id": "poly-order-1",
                "size": "9.25",
                "price": "0.206",
                "fee": "0.0142",
            }
        ]

    def get_open_orders(self, only_first_page: bool = False) -> list[dict[str, object]]:
        self.open_order_requests += 1
        return []

    def get_balance_allowance(self, _params: object) -> dict[str, object]:
        return {"balance": "123000000"}

    def set_api_creds(self, _creds: object) -> None:
        return None


@pytest.mark.asyncio
async def test_polymarket_transport_uses_trade_and_order_queries_for_actual_fills() -> None:
    client = FakeClobClient()
    transport = PolymarketSdkTransport(
        client,
        order_args_factory=lambda **values: SimpleNamespace(**values),
        trade_params_factory=lambda **values: SimpleNamespace(**values),
        balance_params_factory=lambda: object(),
        fok_order_type="FOK",
    )

    result = await transport.create_order(
        {
            "token_id": "token-yes",
            "clientOrderId": "corr-poly",
            "side": "BUY",
            "price": "0.205",
            "size": "10.5",
            "order_type": "FOK",
        },
    )
    balance = await transport.get_available_balance()
    recovered = await transport.get_order_by_client_id("corr-poly")

    assert vars(client.created) == {
        "token_id": "token-yes",
        "price": 0.205,
        "size": 10.5,
        "side": "BUY",
    }
    assert client.posted_type == "FOK"
    assert result == {
        "clientOrderId": "corr-poly",
        "status": "MATCHED",
        "fills": [
            {"id": "trade-1", "size": "9.25", "price": "0.206", "fee": "0.0142"},
        ],
    }
    assert recovered == result
    assert client.order_requests == ["poly-order-1"]
    assert client.trade_requests
    assert balance == Decimal(123)


@pytest.mark.asyncio
async def test_polymarket_transport_keeps_matched_status_without_fake_fill_fee() -> None:
    class FeeUnknownClobClient(FakeClobClient):
        def __init__(self) -> None:
            super().__init__()
            self.delayed_lookup_count = 0

        def get_order(self, order_id: str) -> dict[str, object]:
            self.order_requests.append(order_id)
            return {
                "id": order_id,
                "status": "MATCHED",
                "size_matched": "7.5",
                "associate_trades": ["trade-1", "trade-2"],
            }

        def get_trades(
            self,
            params: object = None,
            only_first_page: bool = False,
        ) -> list[dict[str, object]]:
            self.trade_requests.append((params, only_first_page))
            requested_id = getattr(params, "id", None)
            if requested_id == "trade-2":
                self.delayed_lookup_count += 1
            trades: list[dict[str, object]] = [
                {
                    "id": "trade-1",
                    "taker_order_id": "poly-order-1",
                    "size": "2.5",
                    "price": "0.198",
                    "fee": "0.0035",
                },
                {
                    "id": "trade-2",
                    "taker_order_id": "poly-order-1",
                    "size": "5",
                    "price": "0.199",
                    **({"fee": "0.007"} if self.delayed_lookup_count > 1 else {}),
                },
            ]
            if requested_id is None:
                return trades
            return [trade for trade in trades if trade["id"] == requested_id]

    client = FeeUnknownClobClient()
    transport = PolymarketSdkTransport(
        client,
        order_args_factory=lambda **values: SimpleNamespace(**values),
        trade_params_factory=lambda **values: SimpleNamespace(**values),
        balance_params_factory=lambda: object(),
        fok_order_type="FOK",
    )

    result = await transport.create_order(
        {
            "token_id": "token-yes",
            "clientOrderId": "corr-poly",
            "side": "BUY",
            "price": "0.205",
            "size": "10.5",
            "order_type": "FOK",
        },
    )
    refreshed = await transport.get_order_by_client_id("corr-poly")

    assert result == {
        "clientOrderId": "corr-poly",
        "status": "MATCHED",
        "fills": [
            {"id": "trade-1", "size": "2.5", "price": "0.198", "fee": "0.0035"},
        ],
    }
    assert refreshed == {
        "clientOrderId": "corr-poly",
        "status": "MATCHED",
        "fills": [
            {"id": "trade-1", "size": "2.5", "price": "0.198", "fee": "0.0035"},
            {"id": "trade-2", "size": "5", "price": "0.199", "fee": "0.007"},
        ],
    }


@pytest.mark.asyncio
async def test_polymarket_transport_recovers_client_id_from_recent_open_orders() -> None:
    class RecoveryClobClient(FakeClobClient):
        def get_open_orders(self, only_first_page: bool = False) -> list[dict[str, object]]:
            self.open_order_requests += 1
            assert only_first_page is True
            return [
                {
                    "id": "poly-order-2",
                    "client_order_id": "corr-poly-recovered",
                }
            ]

        def get_order(self, order_id: str) -> dict[str, object]:
            self.order_requests.append(order_id)
            return {
                "id": order_id,
                "status": "FILLED",
                "associate_trades": ["trade-2"],
                "size_matched": "4.5",
            }

        def get_trades(
            self,
            params: object = None,
            only_first_page: bool = False,
        ) -> list[dict[str, object]]:
            self.trade_requests.append((params, only_first_page))
            return [
                {
                    "id": "trade-2",
                    "taker_order_id": "poly-order-2",
                    "size": "4.5",
                    "price": "0.222",
                    "fee_usdc": "0.0099",
                }
            ]

    client = RecoveryClobClient()
    transport = PolymarketSdkTransport(
        client,
        order_args_factory=lambda **values: SimpleNamespace(**values),
        trade_params_factory=lambda **values: SimpleNamespace(**values),
        balance_params_factory=lambda: object(),
        fok_order_type="FOK",
    )

    recovered = await transport.get_order_by_client_id("corr-poly-recovered")

    assert recovered == {
        "clientOrderId": "corr-poly-recovered",
        "status": "FILLED",
        "fills": [
            {"id": "trade-2", "size": "4.5", "price": "0.222", "fee": "0.0099"},
        ],
    }
    assert client.open_order_requests == 1
    assert client.order_requests == ["poly-order-2"]


@pytest.mark.asyncio
async def test_polymarket_transport_from_credentials_uses_v2_api_key_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeApiCreds:
        def __init__(self, *, api_key: str, api_secret: str, api_passphrase: str) -> None:
            self.api_key = api_key
            self.api_secret = api_secret
            self.api_passphrase = api_passphrase

    class FakeBalanceAllowanceParams:
        def __init__(self, *, asset_type: object, signature_type: int) -> None:
            self.asset_type = asset_type
            self.signature_type = signature_type

    class FakeClobClientV2:
        def __init__(
            self,
            host: str,
            *,
            chain_id: int,
            key: str,
            creds: object,
            signature_type: int,
            funder: str,
        ) -> None:
            captured["host"] = host
            captured["chain_id"] = chain_id
            captured["key"] = key
            captured["creds"] = creds
            captured["signature_type"] = signature_type
            captured["funder"] = funder
            self.derived = 0
            self.api_creds: object | None = None

        def create_or_derive_api_key(self) -> FakeApiCreds:
            self.derived += 1
            return FakeApiCreds(
                api_key="derived-key",
                api_secret="derived-secret",
                api_passphrase="derived-passphrase",
            )

        def set_api_creds(self, creds: object) -> None:
            self.api_creds = creds

    fake_package = SimpleNamespace(ClobClient=FakeClobClientV2)
    fake_types = SimpleNamespace(
        ApiCreds=FakeApiCreds,
        AssetType=SimpleNamespace(COLLATERAL="COLLATERAL"),
        BalanceAllowanceParams=FakeBalanceAllowanceParams,
        OrderArgs=object,
        OrderType=SimpleNamespace(FOK="FOK"),
    )
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake_package)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.client", fake_package)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", fake_types)

    transport = await PolymarketSdkTransport.from_credentials(
        base_url="https://clob.polymarket.com/",
        private_key="private-key",
        chain_id=137,
        signature_type=1,
        funder_address="0xfunder",
    )

    assert isinstance(transport, PolymarketSdkTransport)
    assert captured == {
        "host": "https://clob.polymarket.com",
        "chain_id": 137,
        "key": "private-key",
        "creds": None,
        "signature_type": 1,
        "funder": "0xfunder",
    }
    fake_client = cast(Any, transport._client)
    assert fake_client.derived == 1
    assert isinstance(fake_client.api_creds, FakeApiCreds)


@pytest.mark.asyncio
async def test_polymarket_transport_from_credentials_uses_full_v2_l2_triplet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeApiCreds:
        def __init__(self, *, api_key: str, api_secret: str, api_passphrase: str) -> None:
            self.api_key = api_key
            self.api_secret = api_secret
            self.api_passphrase = api_passphrase

    class FakeClobClientV2:
        def __init__(self, host: str, **kwargs: object) -> None:
            captured["host"] = host
            captured.update(kwargs)

        def create_or_derive_api_key(self) -> FakeApiCreds:
            raise AssertionError("derivation should not happen when full L2 creds exist")

    fake_package = SimpleNamespace(ClobClient=FakeClobClientV2)
    fake_types = SimpleNamespace(
        ApiCreds=FakeApiCreds,
        AssetType=SimpleNamespace(COLLATERAL="COLLATERAL"),
        BalanceAllowanceParams=object,
        OrderArgs=object,
        OrderType=SimpleNamespace(FOK="FOK"),
    )
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake_package)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.client", fake_package)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", fake_types)

    await PolymarketSdkTransport.from_credentials(
        base_url="https://clob.polymarket.com/",
        private_key="private-key",
        chain_id=137,
        signature_type=1,
        funder_address="0xfunder",
        api_key="existing-key",
        api_secret="existing-secret",
        passphrase="existing-passphrase",
    )

    assert captured["host"] == "https://clob.polymarket.com"
    assert isinstance(captured["creds"], FakeApiCreds)
    assert captured["creds"].api_key == "existing-key"
    assert captured["creds"].api_secret == "existing-secret"
    assert captured["creds"].api_passphrase == "existing-passphrase"


@pytest.mark.asyncio
async def test_oddpool_probe_uses_official_read_only_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.oddpool.com/arbitrage/current")
        assert request.headers["x-api-key"] == "oddpool-key"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[])

    record = IntegrationConfigRecord(
        provider=IntegrationProvider.ODDPOOL,
        enabled=True,
        environment=IntegrationEnvironment.PRODUCTION,
        base_url="https://api.oddpool.com",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await HttpIntegrationConnectionProbe(client).test(
            record,
            {"api_token": "oddpool-key"},
        )

    assert result.ok is True
    assert result.code == "ODDPOOL_CONNECTION_OK"


@pytest.mark.asyncio
async def test_oddpool_probe_retries_rate_limits_with_bounded_delays() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=[])

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    record = IntegrationConfigRecord(
        provider=IntegrationProvider.ODDPOOL,
        enabled=True,
        environment=IntegrationEnvironment.PRODUCTION,
        base_url="https://api.oddpool.com",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await HttpIntegrationConnectionProbe(
            client,
            sleeper=record_sleep,
        ).test(record, {"api_token": "oddpool-key"})

    assert attempts == 3
    assert delays == [1, 2]
    assert result.ok is True
    assert result.code == "ODDPOOL_CONNECTION_OK"


@pytest.mark.asyncio
async def test_connection_probe_uses_authenticated_account_transports() -> None:
    observed: list[tuple[str, dict[str, str]]] = []

    class AccountTransport:
        async def get_available_balance(self) -> Decimal:
            return Decimal("25.50")

    class ReadOnlyTransport:
        async def probe_read_only(self) -> None:
            return None

    async def kalshi_factory(
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> AccountTransport:
        observed.append((record.provider.value, secrets))
        return AccountTransport()

    async def polymarket_factory(
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> ReadOnlyTransport:
        observed.append((record.provider.value, secrets))
        return ReadOnlyTransport()

    async with httpx.AsyncClient() as client:
        probe = HttpIntegrationConnectionProbe(
            client,
            kalshi_factory=kalshi_factory,
            polymarket_factory=polymarket_factory,
        )
        kalshi = await probe.test(
            IntegrationConfigRecord(
                provider=IntegrationProvider.KALSHI,
                enabled=True,
                environment=IntegrationEnvironment.PRODUCTION,
                base_url="https://kalshi.test",
                configuration={"key_id": "key-id"},
            ),
            {"private_key": "private-key"},
        )
        polymarket = await probe.test(
            IntegrationConfigRecord(
                provider=IntegrationProvider.POLYMARKET,
                enabled=True,
                environment=IntegrationEnvironment.PRODUCTION,
                base_url="https://clob.test",
                configuration={
                    "chain_id": 137,
                    "signature_type": "1",
                    "funder_address": "0xfunder",
                    "wallet_address": "0xwallet",
                },
            ),
            {"private_key": "private-key"},
        )

    assert kalshi.ok is True
    assert kalshi.code == "KALSHI_BALANCE_ACCESS_OK"
    assert kalshi.detail == "authenticated Kalshi balance access succeeded"
    assert polymarket.ok is True
    assert polymarket.code == "POLYMARKET_READ_ONLY_OK"
    assert polymarket.detail == "Polymarket read-only authentication succeeded"
    assert [provider for provider, _ in observed] == ["kalshi", "polymarket"]


def test_connection_probe_uses_polymarket_read_only_sdk_calls_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeReadOnlyClient:
        def __init__(self) -> None:
            self.balance_allowance_calls = 0
            self.open_orders_calls = 0
            self.trades_calls = 0
            self.create_order_calls = 0
            self.post_order_calls = 0
            self.cancel_calls = 0
            self.update_allowance_calls = 0

        def get_balance_allowance(self, params: object) -> dict[str, object]:
            self.balance_allowance_calls += 1
            assert params is not None
            return {"balance": "123000000"}

        def get_open_orders(
            self,
            params: object = None,
            only_first_page: bool = False,
            next_cursor: str | None = None,
        ) -> list[dict[str, object]]:
            self.open_orders_calls += 1
            assert params is None
            assert only_first_page is True
            assert next_cursor is None
            return []

        def get_order(self, order_id: str) -> dict[str, object]:
            return {"id": order_id, "status": "ORDER_STATUS_MATCHED"}

        def get_trades(
            self,
            params: object = None,
            only_first_page: bool = False,
        ) -> list[dict[str, object]]:
            self.trades_calls += 1
            assert params is None
            assert only_first_page is True
            return []

        def create_order(self, order: object) -> object:
            self.create_order_calls += 1
            return order

        def post_order(self, order: object, order_type: object) -> dict[str, object]:
            self.post_order_calls += 1
            return {"order": order, "order_type": order_type}

        def cancel(self, order_id: str) -> None:
            self.cancel_calls += 1

        def update_balance_allowance(self, params: object) -> None:
            self.update_allowance_calls += 1

        def set_api_creds(self, creds: object) -> None:
            return None

    fake_client = FakeReadOnlyClient()
    credential_arguments: dict[str, object] = {}
    fake_transport = PolymarketSdkTransport(
        fake_client,
        order_args_factory=lambda **values: SimpleNamespace(**values),
        trade_params_factory=lambda **values: SimpleNamespace(**values),
        balance_params_factory=lambda: object(),
        fok_order_type="FOK",
    )

    async def fake_from_credentials(**values: object) -> PolymarketSdkTransport:
        credential_arguments.update(values)
        return fake_transport

    monkeypatch.setattr(
        PolymarketSdkTransport,
        "from_credentials",
        staticmethod(fake_from_credentials),
    )

    async def exercise() -> tuple[ConnectionTestResult, FakeReadOnlyClient]:
        async with httpx.AsyncClient() as client:
            probe = HttpIntegrationConnectionProbe(client)
            result = await probe.test(
                IntegrationConfigRecord(
                    provider=IntegrationProvider.POLYMARKET,
                    enabled=True,
                    environment=IntegrationEnvironment.PRODUCTION,
                    base_url="https://clob.test",
                    configuration={
                        "chain_id": 137,
                        "signature_type": 1,
                        "funder_address": "0xfunder",
                    },
                ),
                {"private_key": "private-key"},
            )
        return result, fake_client

    result, counters = asyncio.run(exercise())

    assert result.ok is True
    assert result.code == "POLYMARKET_READ_ONLY_OK"
    assert result.detail == "Polymarket read-only authentication succeeded"
    assert counters.balance_allowance_calls == 1
    assert counters.open_orders_calls == 1
    assert counters.trades_calls == 1
    assert counters.create_order_calls == 0
    assert counters.post_order_calls == 0
    assert counters.cancel_calls == 0
    assert counters.update_allowance_calls == 0
    assert credential_arguments["derive_only"] is True


def test_connection_probe_sanitizes_polymarket_read_only_failures() -> None:
    secret = "private-key-secret"

    class FailingReadOnlyTransport:
        async def probe_read_only(self) -> None:
            raise RuntimeError(f"sdk rejected {secret}")

    async def polymarket_factory(
        record: IntegrationConfigRecord,
        secrets: dict[str, str],
    ) -> FailingReadOnlyTransport:
        assert record.provider is IntegrationProvider.POLYMARKET
        assert secrets["private_key"] == secret
        return FailingReadOnlyTransport()

    async def exercise() -> ConnectionTestResult:
        async with httpx.AsyncClient() as client:
            probe = HttpIntegrationConnectionProbe(
                client,
                polymarket_factory=polymarket_factory,
            )
            return await probe.test(
                IntegrationConfigRecord(
                    provider=IntegrationProvider.POLYMARKET,
                    enabled=True,
                    environment=IntegrationEnvironment.PRODUCTION,
                    base_url="https://clob.test",
                    configuration={
                        "chain_id": 137,
                        "signature_type": 1,
                        "funder_address": "0xfunder",
                    },
                ),
                {"private_key": secret},
            )

    result = asyncio.run(exercise())

    assert result.ok is False
    assert result.code == "POLYMARKET_READ_ONLY_FAILED"
    assert result.detail == "Polymarket read-only authentication failed"
    assert secret not in result.detail
