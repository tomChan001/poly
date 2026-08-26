import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

import httpx

from backend.app.adapters.kalshi.orderbook import parse_kalshi_book
from backend.app.adapters.polymarket.orderbook import parse_polymarket_book
from backend.app.domain.market import NormalizedBook
from backend.app.services.executable_pairs import ExecutablePair


class NativeMarketDataClient:
    """Reads execution prices directly from each venue's native order book."""

    def __init__(
        self,
        *,
        kalshi_base_url: str,
        polymarket_base_url: str,
        http_client: httpx.AsyncClient,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._kalshi_base_url = kalshi_base_url.rstrip("/")
        self._polymarket_base_url = polymarket_base_url.rstrip("/")
        self._http = http_client
        self._clock = clock or (lambda: datetime.now(UTC))

    async def get_books(
        self,
        pair: ExecutablePair,
        now: datetime,
    ) -> tuple[NormalizedBook, NormalizedBook]:
        kalshi_result, polymarket_result = await asyncio.gather(
            self._get_with_receipt_time(
                f"{self._kalshi_base_url}/trade-api/v2/markets/"
                f"{pair.kalshi_market_id}/orderbook"
            ),
            self._get_with_receipt_time(
                f"{self._polymarket_base_url}/book",
                params={"token_id": pair.polymarket_market_id},
            ),
        )
        kalshi_response, kalshi_received_at = kalshi_result
        polymarket_response, polymarket_received_at = polymarket_result
        kalshi_response.raise_for_status()
        polymarket_response.raise_for_status()

        kalshi_payload = _object_payload(kalshi_response)
        polymarket_payload = _object_payload(polymarket_response)
        normalized_kalshi = _normalize_kalshi_payload(kalshi_payload)
        return (
            parse_kalshi_book(
                pair.kalshi_market_id,
                pair.kalshi_outcome,
                normalized_kalshi,
                captured_at=_capture_time(kalshi_payload),
                received_at=kalshi_received_at,
            ),
            parse_polymarket_book(
                pair.polymarket_market_id,
                pair.polymarket_outcome,
                polymarket_payload,
                received_at=polymarket_received_at,
            ),
        )

    async def _get_with_receipt_time(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> tuple[httpx.Response, datetime]:
        response = await self._http.get(url, params=params)
        return response, self._clock()


def _object_payload(response: httpx.Response) -> dict[str, object]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError("native order book response must be an object")
    return payload


def _normalize_kalshi_payload(payload: dict[str, object]) -> dict[str, object]:
    raw_book = payload.get("orderbook", payload)
    if not isinstance(raw_book, dict):
        raise TypeError("Kalshi response is missing orderbook data")

    normalized: dict[str, object] = {
        "yes": _kalshi_levels(raw_book.get("yes", [])),
        "no": _kalshi_levels(raw_book.get("no", [])),
    }
    sequence = payload.get("sequence") or raw_book.get("sequence")
    if sequence is None:
        # Some REST responses do not expose a sequence. A canonical content
        # hash still gives the runtime a stable identity for duplicate guards.
        encoded = json.dumps(raw_book, sort_keys=True, separators=(",", ":"), default=str)
        sequence = f"sha256:{sha256(encoded.encode('utf-8')).hexdigest()}"
    normalized["sequence"] = sequence
    return normalized


def _capture_time(payload: dict[str, object]) -> datetime | None:
    """Return only an exchange-provided capture time; never relabel receipt time."""
    raw_value = payload.get("timestamp") or payload.get("captured_at")
    if isinstance(raw_value, (int, str)) and not isinstance(raw_value, bool):
        value = str(raw_value)
        if value.isdigit():
            divisor = 1000 if len(value) >= 13 else 1
            return datetime.fromtimestamp(int(value) / divisor, tz=UTC)
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _kalshi_levels(value: object) -> list[tuple[str, object]]:
    if not isinstance(value, list):
        raise TypeError("Kalshi order book side must be a list")
    levels: list[tuple[str, object]] = []
    for level in value:
        if not isinstance(level, list) or len(level) != 2:
            raise TypeError("Kalshi order book level must contain price and quantity")
        price, quantity = level
        if not isinstance(price, (str, int)) or isinstance(price, bool):
            raise TypeError("Kalshi order book price must be integer cents")
        # The domain decimal parser deliberately accepts text instead of an
        # already-created Decimal, keeping every external numeric boundary
        # consistent and rejecting accidental binary floats.
        normalized_price = Decimal(price) / Decimal(100)
        levels.append((format(normalized_price, "f"), quantity))
    return levels
