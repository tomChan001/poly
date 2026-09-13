"""Public, per-pair fee snapshots for indicative quotes before mapping review."""

import asyncio
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx

from backend.app.domain.enums import Venue
from backend.app.services.executable_pairs import ExecutablePair
from backend.app.services.fees import (
    FeeEngine,
    KalshiTakerFeeRule,
    ProbabilityCurveFeeRule,
)
from backend.app.services.optimizer import QuoteOptimizer


class NativePreviewFeeProvider:
    """Fetch current native parameters without changing the execution fee engine.

    Estimates assume taker orders and no builder surcharge. The optimizer uses
    VWAP: for supported quadratic curves this overestimates unrounded sweep
    fees, but exchange rounding on individual fills can still change the total.
    """

    def __init__(
        self,
        *,
        kalshi_base_url: str,
        polymarket_base_url: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._kalshi = kalshi_base_url.rstrip("/")
        self._polymarket = polymarket_base_url.rstrip("/")
        self._http = http_client

    async def optimizer_for(self, pair: ExecutablePair) -> QuoteOptimizer:
        try:
            kalshi, polymarket = await asyncio.gather(
                self._kalshi_rule(pair.kalshi_market_id),
                self._polymarket_rule(pair.polymarket_market_id),
            )
        except TypeError as exc:
            raise ValueError("native fee metadata has an invalid shape") from exc
        fees = FeeEngine()
        fees.register(Venue.KALSHI, pair.kalshi_category, kalshi)
        fees.register(Venue.POLYMARKET, pair.polymarket_category, polymarket)
        return QuoteOptimizer(fees)

    async def _get(self, url: str) -> dict[str, object]:
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            payload = response.json(parse_float=Decimal)
        except (httpx.HTTPError, ValueError) as exc:
            raise ValueError("native fee metadata is unavailable") from exc
        return _object(payload)

    async def _kalshi_rule(self, market_id: str) -> KalshiTakerFeeRule:
        # Event overrides take precedence over the series. Do not infer a
        # series from ticker prefixes or use the UI's broad market category.
        # https://docs.kalshi.com/api-reference/events/get-event
        # https://docs.kalshi.com/api-reference/market/get-series
        market = _object((await self._get(
            f"{self._kalshi}/trade-api/v2/markets/{quote(market_id, safe='')}"
        )).get("market"))
        event_id = _identifier(market.get("event_ticker"))
        event = _object((await self._get(
            f"{self._kalshi}/trade-api/v2/events/{quote(event_id, safe='')}"
        )).get("event"))
        series_id = _identifier(event.get("series_ticker"))
        series = _object((await self._get(
            f"{self._kalshi}/trade-api/v2/series/{quote(series_id, safe='')}"
        )).get("series"))

        fee_type = event.get("fee_type_override")
        if fee_type is None:
            fee_type = series.get("fee_type")
        multiplier = event.get("fee_multiplier_override")
        if multiplier is None:
            multiplier = series.get("fee_multiplier")
        if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
            raise ValueError("unsupported Kalshi fee model")
        rate = _nonnegative_decimal(multiplier) * Decimal("0.07")
        # General taker formula: M * .07 * C * P * (1-P).
        # https://kalshi.com/docs/kalshi-fee-schedule.pdf (effective July 7, 2026)
        return KalshiTakerFeeRule(f"kalshi:{market_id}:{fee_type}:{rate}", rate)

    async def _polymarket_rule(self, token_id: str) -> ProbabilityCurveFeeRule:
        # These public V2 routes and fd fields are used by the official
        # py-clob-client-v2 client, including installed version 1.1.0.
        # https://docs.polymarket.com/v2-migration
        # https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/client.py
        token_market = await self._get(
            f"{self._polymarket}/markets-by-token/{quote(token_id, safe='')}"
        )
        condition_id = _identifier(token_market.get("condition_id"))
        market = await self._get(
            f"{self._polymarket}/clob-markets/{quote(condition_id, safe='')}"
        )
        tokens = market.get("t")
        if not isinstance(tokens, list) or not any(
            isinstance(token, dict) and token.get("t") == token_id for token in tokens
        ):
            raise ValueError("Polymarket fee metadata does not match the outcome token")
        details = _object(market.get("fd"))
        rate = _nonnegative_decimal(details.get("r"))
        exponent = _nonnegative_decimal(details.get("e"))
        if not isinstance(details.get("to"), bool):
            raise TypeError("missing Polymarket fee applicability")
        if rate != 0 and exponent != 1:
            raise ValueError("unsupported Polymarket fee exponent")
        # Five decimal places, rounded upward for an indicative estimate.
        # https://docs.polymarket.com/trading/fees
        return ProbabilityCurveFeeRule(
            f"polymarket-v2:{condition_id}:{rate}:{exponent}",
            rate,
            minimum_currency_unit=Decimal("0.00001"),
        )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("native fee metadata must be an object")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("native fee market identifier is missing")
    return value


def _nonnegative_decimal(value: object) -> Decimal:
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise TypeError("native fee parameter must be numeric")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("native fee parameter must be numeric") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("native fee parameter must be finite and nonnegative")
    return result
