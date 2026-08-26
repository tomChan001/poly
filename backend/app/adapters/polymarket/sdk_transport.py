import asyncio
import importlib
import sys
from collections.abc import Callable, Iterable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Protocol


class ClobClientPort(Protocol):
    def create_order(self, order: object) -> object: ...

    def post_order(self, order: object, order_type: object) -> dict[str, object]: ...

    def get_order(self, order_id: str) -> dict[str, object]: ...

    def get_open_orders(self, only_first_page: bool = False) -> list[dict[str, object]]: ...

    def get_trades(
        self,
        params: object = None,
        only_first_page: bool = False,
    ) -> list[dict[str, object]]: ...

    def get_balance_allowance(self, params: object) -> dict[str, object]: ...

    def set_api_creds(self, creds: object) -> None: ...


class PolymarketSdkTransport:
    """Async boundary around the synchronous official Polymarket CLOB SDK."""

    def __init__(
        self,
        client: ClobClientPort,
        *,
        order_args_factory: Callable[..., object],
        trade_params_factory: Callable[..., object],
        balance_params_factory: Callable[[], object],
        fok_order_type: object,
    ) -> None:
        self._client = client
        self._order_args_factory = order_args_factory
        self._trade_params_factory = trade_params_factory
        self._balance_params_factory = balance_params_factory
        self._fok_order_type = fok_order_type
        self._known_orders: dict[str, dict[str, object]] = {}
        self._client_to_order_id: dict[str, str] = {}
        self._order_to_client_id: dict[str, str] = {}
        self._client_to_trade_ids: dict[str, tuple[str, ...]] = {}
        self._trade_to_client_id: dict[str, str] = {}
        self._incomplete_orders: set[str] = set()

    @classmethod
    async def from_credentials(
        cls,
        *,
        base_url: str,
        private_key: str,
        chain_id: int,
        signature_type: int,
        funder_address: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
    ) -> "PolymarketSdkTransport":
        from py_clob_client_v2.client import ClobClient  # type: ignore[import-untyped]  # noqa: I001

        clob_types = sys.modules.get("py_clob_client_v2.clob_types")
        if clob_types is None:
            clob_types = importlib.import_module("py_clob_client_v2.clob_types")

        ApiCreds = clob_types.ApiCreds
        AssetType = clob_types.AssetType
        BalanceAllowanceParams = clob_types.BalanceAllowanceParams
        OrderArgs = clob_types.OrderArgs
        OrderType = clob_types.OrderType
        TradeParams = getattr(
            clob_types,
            "TradeParams",
            lambda **kwargs: SimpleNamespace(**kwargs),
        )

        credentials = None
        if api_key and api_secret and passphrase:
            credentials = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=passphrase,
            )
        client = ClobClient(
            base_url.rstrip("/"),
            chain_id=chain_id,
            key=private_key,
            creds=credentials,
            signature_type=signature_type,
            funder=funder_address,
        )
        if credentials is None:
            derived = await asyncio.to_thread(client.create_or_derive_api_key)
            client.set_api_creds(derived)
        return cls(
            client,
            order_args_factory=OrderArgs,
            trade_params_factory=TradeParams,
            balance_params_factory=lambda: BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=signature_type,
            ),
            fok_order_type=OrderType.FOK,
        )

    async def create_order(self, payload: dict[str, object]) -> dict[str, object]:
        client_order_id = _required_text(payload, "clientOrderId")
        size = _required_text(payload, "size")
        price = _required_text(payload, "price")
        order_args = self._order_args_factory(
            token_id=_required_text(payload, "token_id"),
            price=float(price),
            size=float(size),
            side=_required_text(payload, "side"),
        )
        signed = await asyncio.to_thread(self._client.create_order, order_args)
        response = await asyncio.to_thread(
            self._client.post_order,
            signed,
            self._fok_order_type,
        )
        if not isinstance(response, dict):
            raise TypeError("Polymarket order response must be an object")

        order_id = _optional_text(response.get("orderID") or response.get("orderId"))
        trade_ids = _trade_ids_from_response(response)
        self._remember_observation(client_order_id, order_id, trade_ids)
        normalized = await self._normalize_order_snapshot(
            client_order_id,
            response.get("status"),
            order_id,
            trade_ids,
        )
        self._known_orders[client_order_id] = normalized
        return normalized

    async def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> dict[str, object] | None:
        known = self._known_orders.get(client_order_id)
        if known is not None and client_order_id not in self._incomplete_orders:
            return known

        order_id = self._client_to_order_id.get(client_order_id)
        if order_id is None:
            order_id = await asyncio.to_thread(self._recover_order_id_from_open_orders, client_order_id)
            if order_id is None:
                return None

        normalized = await self._normalize_order_snapshot(
            client_order_id,
            None,
            order_id,
            self._client_to_trade_ids.get(client_order_id, ()),
        )
        self._known_orders[client_order_id] = normalized
        return normalized

    async def get_available_balance(self) -> Decimal:
        payload = await asyncio.to_thread(
            self._client.get_balance_allowance,
            self._balance_params_factory(),
        )
        raw_balance = payload.get("balance")
        if not isinstance(raw_balance, (str, int)) or isinstance(raw_balance, bool):
            raise TypeError("Polymarket collateral balance must be an integer string")
        return Decimal(raw_balance) / Decimal(1_000_000)

    def _remember_observation(
        self,
        client_order_id: str,
        order_id: str | None,
        trade_ids: Iterable[str],
    ) -> None:
        if order_id is not None:
            self._client_to_order_id[client_order_id] = order_id
            self._order_to_client_id[order_id] = client_order_id
        unique_trade_ids = tuple(dict.fromkeys(trade_id for trade_id in trade_ids if trade_id))
        if unique_trade_ids:
            self._client_to_trade_ids[client_order_id] = unique_trade_ids
            for trade_id in unique_trade_ids:
                self._trade_to_client_id[trade_id] = client_order_id

    async def _normalize_order_snapshot(
        self,
        client_order_id: str,
        fallback_status: object,
        order_id: str | None,
        hinted_trade_ids: Iterable[str],
    ) -> dict[str, object]:
        order_payload: dict[str, object] | None = None
        if order_id is not None:
            order_payload = await asyncio.to_thread(self._client.get_order, order_id)
            if not isinstance(order_payload, dict):
                raise TypeError("Polymarket order lookup must return an object")

        trade_ids = list(
            dict.fromkeys(
                [trade_id for trade_id in hinted_trade_ids if trade_id]
                + (_trade_ids_from_response(order_payload) if order_payload is not None else [])
            )
        )

        fills = await self._load_actual_fills(order_id, trade_ids)
        raw_status = (
            order_payload.get("status")
            if order_payload is not None and "status" in order_payload
            else fallback_status
        )
        status = _normalize_order_status(raw_status)
        observed_trade_ids = set(trade_ids)
        normalized_trade_ids = {fill["id"] for fill in fills}
        if status in {"MATCHED", "FILLED", "PARTIAL"} and (
            not observed_trade_ids or not observed_trade_ids.issubset(normalized_trade_ids)
        ):
            self._incomplete_orders.add(client_order_id)
        else:
            self._incomplete_orders.discard(client_order_id)
        return {
            "clientOrderId": client_order_id,
            "status": status,
            "fills": fills,
        }

    async def _load_actual_fills(
        self,
        order_id: str | None,
        trade_ids: Iterable[str],
    ) -> list[dict[str, str]]:
        seen_trade_ids = list(dict.fromkeys(trade_id for trade_id in trade_ids if trade_id))
        fills: list[dict[str, str]] = []

        for trade_id in seen_trade_ids:
            trades = await asyncio.to_thread(
                self._client.get_trades,
                self._trade_params_factory(id=trade_id),
                True,
            )
            fills.extend(self._normalize_trade_batch(trades, order_id, {trade_id}))

        if fills or order_id is None:
            return fills

        recent_trades = await asyncio.to_thread(self._client.get_trades, None, True)
        return self._normalize_trade_batch(recent_trades, order_id, None)

    def _normalize_trade_batch(
        self,
        trades: object,
        order_id: str | None,
        allowed_trade_ids: set[str] | None,
    ) -> list[dict[str, str]]:
        if not isinstance(trades, list):
            raise TypeError("Polymarket trades lookup must return a list")

        fills: list[dict[str, str]] = []
        for trade in trades:
            if not isinstance(trade, dict):
                raise TypeError("Polymarket trade must be an object")
            trade_id = _required_text(trade, "id")
            if allowed_trade_ids is not None and trade_id not in allowed_trade_ids:
                continue
            if order_id is not None and not _trade_matches_order(trade, order_id):
                continue
            fee = _optional_numeric_text(trade.get("fee")) or _optional_numeric_text(
                trade.get("fee_usdc")
            )
            if fee is None:
                continue
            fills.append(
                {
                    "id": trade_id,
                    "size": _required_text(trade, "size"),
                    "price": _required_text(trade, "price"),
                    "fee": fee,
                }
            )
        return fills

    def _recover_order_id_from_open_orders(self, client_order_id: str) -> str | None:
        orders = self._client.get_open_orders(only_first_page=True)
        if not isinstance(orders, list):
            raise TypeError("Polymarket open orders lookup must return a list")
        for order in orders:
            if not isinstance(order, dict):
                raise TypeError("Polymarket open order must be an object")
            observed_client_id = _optional_text(
                order.get("client_order_id") or order.get("clientOrderId")
            )
            observed_order_id = _optional_text(
                order.get("id") or order.get("orderID") or order.get("orderId")
            )
            if observed_client_id is None or observed_order_id is None:
                continue
            self._remember_observation(observed_client_id, observed_order_id, ())
            if observed_client_id == client_order_id:
                return observed_order_id
        return None


def _required_text(payload: dict[str, object], name: str) -> str:
    value: Any = payload.get(name)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value:
        raise TypeError(f"Polymarket {name} must be a non-empty string")
    return value


def _optional_text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        text = str(value)
        return text if text else None
    return None


def _optional_numeric_text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _trade_ids_from_response(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    trade_ids = payload.get("tradeIDs") or payload.get("associate_trades") or []
    if not isinstance(trade_ids, list):
        return []
    return [trade_id for trade_id in trade_ids if isinstance(trade_id, str) and trade_id]


def _trade_matches_order(trade: dict[str, object], order_id: str) -> bool:
    if _optional_text(trade.get("taker_order_id")) == order_id:
        return True
    maker_orders = trade.get("maker_orders")
    if not isinstance(maker_orders, list):
        return False
    for maker_order in maker_orders:
        if not isinstance(maker_order, dict):
            continue
        if _optional_text(
            maker_order.get("order_id") or maker_order.get("id") or maker_order.get("orderID")
        ) == order_id:
            return True
    return False


def _normalize_order_status(value: object) -> str:
    raw = str(value or "UNKNOWN").upper()
    if raw.startswith("ORDER_STATUS_"):
        raw = raw.removeprefix("ORDER_STATUS_")
    return {
        "MATCHED": "MATCHED",
        "FILLED": "FILLED",
        "PARTIAL": "PARTIAL",
        "CANCELED": "CANCELED",
        "CANCELLED": "CANCELED",
        "CANCELED_MARKET_RESOLVED": "CANCELED",
        "INVALID": "REJECTED",
        "REJECTED": "REJECTED",
    }.get(raw, "UNKNOWN")

