import httpx

from backend.app.adapters.oddpool.schema import OddpoolResponse

_RETRY_DELAYS = (1, 2, 4, 8, 16, 60)


def retry_delay_seconds(attempt: int) -> int:
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    return _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]


class OddpoolClient:
    """Thin discovery client; its prices never enter the execution calculator."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._http = http_client

    async def fetch_opportunities(self) -> OddpoolResponse:
        response = await self._http.get(
            f"{self._base_url}/api/opportunities",
            headers={"Authorization": f"Bearer {self._api_token}"},
        )
        response.raise_for_status()
        return OddpoolResponse.model_validate(response.json())

