import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx
from pydantic import ValidationError

from backend.app.adapters.oddpool.schema import OddpoolArbitrageRow, OddpoolResponse
from backend.app.services.integration_config import ODDPOOL_BASE_URL

_RETRY_DELAYS = (1, 2, 4, 8, 16, 60)
Sleeper = Callable[[float], Awaitable[None]]


def retry_delay_seconds(attempt: int) -> int:
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    return _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]


class OddpoolClient:
    """Thin discovery client; its prices never enter the execution calculator."""

    def __init__(
        self,
        api_token: str,
        http_client: httpx.AsyncClient,
        *,
        sleeper: Sleeper = asyncio.sleep,
        max_attempts: int = 6,
    ) -> None:
        self._api_token = api_token
        self._http = http_client
        self._sleeper = sleeper
        self._max_attempts = max_attempts

    async def fetch_opportunities(self) -> OddpoolResponse:
        response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            response = await self._http.get(
                f"{ODDPOOL_BASE_URL}/arbitrage/current",
                headers={"X-API-Key": self._api_token},
            )
            if response.status_code != 429 or attempt == self._max_attempts - 1:
                break
            await self._sleeper(retry_delay_seconds(attempt))
        if response is None:
            raise ValueError("Oddpool max_attempts must be positive")
        response.raise_for_status()
        payload = json.loads(response.text, parse_float=Decimal)
        if not isinstance(payload, list):
            raise TypeError("Oddpool arbitrage response must be a list")

        opportunities = []
        errors = []
        for index, item in enumerate(payload):
            try:
                opportunity = OddpoolArbitrageRow.model_validate(item).to_opportunity()
            except (ValidationError, ValueError, TypeError):
                errors.append(f"row {index}: invalid Oddpool arbitrage row")
                continue
            if opportunity is not None:
                opportunities.append(opportunity)
        return OddpoolResponse(opportunities=opportunities, errors=tuple(errors))

