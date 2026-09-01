import base64
import time
from collections.abc import Callable
from decimal import Decimal
from typing import cast

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiHttpTransport:
    """Authenticated async transport for Kalshi portfolio endpoints."""

    def __init__(
        self,
        base_url: str,
        key_id: str,
        private_key_pem: str,
        http_client: httpx.AsyncClient,
        *,
        timestamp_ms: Callable[[], int] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key_id = key_id
        loaded = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise TypeError("Kalshi private key must be an RSA PEM key")
        self._private_key = cast(rsa.RSAPrivateKey, loaded)
        self._http = http_client
        self._timestamp_ms = timestamp_ms or (lambda: int(time.time() * 1000))

    async def create_order(self, payload: dict[str, object]) -> dict[str, object]:
        return await self._request(
            "POST",
            "/trade-api/v2/portfolio/orders",
            json=payload,
        )

    async def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> dict[str, object] | None:
        # Kalshi does not expose a direct client-ID lookup. Searching the recent
        # order window is recovery only; failure must remain UNKNOWN upstream.
        payload = await self._request(
            "GET",
            "/trade-api/v2/portfolio/orders",
            params={"limit": "200"},
        )
        raw_orders = payload.get("orders", [])
        if not isinstance(raw_orders, list):
            raise TypeError("Kalshi orders response must contain a list")
        for raw_order in raw_orders:
            if isinstance(raw_order, dict) and raw_order.get("client_order_id") == client_order_id:
                return {"order": raw_order}
        return None

    async def get_available_balance(self) -> Decimal:
        payload = await self._request("GET", "/trade-api/v2/portfolio/balance")
        raw_balance = payload.get("balance")
        if not isinstance(raw_balance, (str, int)) or isinstance(raw_balance, bool):
            raise TypeError("Kalshi balance must be integer cents")
        return Decimal(raw_balance) / Decimal(100)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, object]:
        timestamp = str(self._timestamp_ms())
        message = f"{timestamp}{method.upper()}{path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        headers = {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        }
        try:
            response = await self._http.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                json=json,
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError("Kalshi request timed out") from exc
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("Kalshi response must be an object")
        return payload
