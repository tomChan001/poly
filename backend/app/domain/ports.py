from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from backend.app.domain.market import NormalizedBook


class MarketDataPort(Protocol):
    async def get_market(self, market_id: str) -> object: ...

    async def get_book(self, market_id: str, outcome: str) -> NormalizedBook: ...

    def stream_books(self, market_ids: set[str]) -> AsyncIterator[NormalizedBook]: ...


class TradingPort(Protocol):
    async def get_available_balance(self) -> Decimal: ...

    async def submit_fok(self, request: object) -> object: ...

    async def cancel(self, venue_order_id: str) -> None: ...

    async def list_open_orders(self) -> list[object]: ...

    async def list_recent_fills(self, since: datetime) -> list[object]: ...

