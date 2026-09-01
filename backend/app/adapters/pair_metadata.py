import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from backend.app.adapters.kalshi.markets import normalize_kalshi_market
from backend.app.adapters.oddpool.schema import OddpoolLeg, OddpoolOpportunity
from backend.app.domain.enums import Venue
from backend.app.services.executable_pairs import (
    ExecutablePairInput,
    build_pair_fingerprints,
)


class NativePairMetadataResolver:
    """Resolves Oddpool links into native IDs and current venue rules."""

    def __init__(
        self,
        *,
        kalshi_base_url: str,
        polymarket_gamma_url: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._kalshi_base_url = kalshi_base_url.rstrip("/")
        self._polymarket_gamma_url = polymarket_gamma_url.rstrip("/")
        self._http = http_client

    async def resolve(self, opportunity: OddpoolOpportunity) -> ExecutablePairInput:
        legs = {leg.venue: leg for leg in opportunity.legs}
        kalshi_leg = legs[Venue.KALSHI]
        polymarket_leg = legs[Venue.POLYMARKET]
        ticker = kalshi_leg.market_ref
        slug = polymarket_leg.market_ref

        kalshi_response = await self._http.get(
            f"{self._kalshi_base_url}/trade-api/v2/markets/{ticker}"
        )
        polymarket_response = await self._http.get(
            f"{self._polymarket_gamma_url}/markets",
            params={"slug": slug},
        )
        kalshi_response.raise_for_status()
        polymarket_response.raise_for_status()

        kalshi_payload = _wrapped_object(kalshi_response.json(), "market")
        kalshi = normalize_kalshi_market(kalshi_payload)
        polymarket_candidates = _object_list(polymarket_response.json(), "markets")
        if not polymarket_candidates:
            event_response = await self._http.get(
                f"{self._polymarket_gamma_url}/events",
                params={"slug": slug},
            )
            event_response.raise_for_status()
            polymarket_candidates = _event_markets(event_response.json())
        polymarket = _select_polymarket_market(
            polymarket_candidates,
            opportunity.title,
            polymarket_leg.source_condition_id,
        )
        token_id = _polymarket_token(polymarket, polymarket_leg.outcome)
        native_condition_id = _optional_text(
            polymarket,
            "conditionId",
            "condition_id",
        )
        if (
            polymarket_leg.source_condition_id is not None
            and native_condition_id != polymarket_leg.source_condition_id
        ):
            raise ValueError("Polymarket condition ID mismatch")
        if (
            polymarket_leg.source_token_id is not None
            and token_id != polymarket_leg.source_token_id
        ):
            raise ValueError("Polymarket token ID mismatch")
        polymarket_rule = _required_text(polymarket, "description")
        polymarket_minimum = _decimal_field(
            polymarket,
            "orderMinSize",
            "minimum_order_size",
        )
        kalshi_settlement = _datetime_field(
            kalshi_payload,
            "settlement_date",
            "expiration_time",
            "close_date",
            "end_date",
        )
        polymarket_settlement = _datetime_field(
            polymarket,
            "endDate",
            "end_date",
            "resolutionDate",
            "settlementDate",
        )

        # Kalshi accepts whole contract counts, so the shared quantity step is
        # one contract even when Polymarket exposes finer token quantities.
        pair = ExecutablePairInput(
            title=opportunity.title,
            kalshi_market_id=kalshi.external_id,
            kalshi_outcome=_outcome(kalshi_leg),
            kalshi_rule_text=kalshi.rule_text,
            kalshi_rule_url=(
                kalshi_leg.market_url or f"https://kalshi.com/markets/{ticker}"
            ),
            polymarket_market_id=token_id,
            polymarket_outcome=_outcome(polymarket_leg),
            polymarket_rule_text=polymarket_rule,
            polymarket_rule_url=(
                polymarket_leg.market_url or f"https://polymarket.com/event/{slug}"
            ),
            minimum_quantity=max(kalshi.minimum_quantity, polymarket_minimum, Decimal(1)),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_expected_settlement_at=kalshi_settlement,
            polymarket_expected_settlement_at=polymarket_settlement,
            worst_case_settlement_at=max(
                [value for value in (kalshi_settlement, polymarket_settlement) if value is not None],
                default=None,
            ),
            kalshi_category=_text_field(kalshi_payload, "category", "series_ticker", "seriesTicker"),
            polymarket_category=_text_field(polymarket, "category", "seriesSlug", "groupItemTitle"),
            kalshi_minimum_tick=kalshi.minimum_tick,
            polymarket_minimum_tick=_decimal_field(
                polymarket,
                "orderPriceMinTickSize",
                "minimum_tick_size",
            ),
        )
        native_fingerprint, material_fingerprint = build_pair_fingerprints(pair)
        return ExecutablePairInput(
            **{
                **asdict(pair),
                "native_fingerprint": native_fingerprint,
                "material_fingerprint": material_fingerprint,
            }
        )


def _wrapped_object(payload: object, name: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise TypeError("venue metadata response must be an object")
    value = payload.get(name, payload)
    if not isinstance(value, dict):
        raise TypeError(f"venue metadata response is missing {name}")
    return value


def _object_list(payload: object, name: str) -> list[dict[str, object]]:
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise TypeError(f"Polymarket {name} response must be an object list")
    return payload


def _event_markets(payload: object) -> list[dict[str, object]]:
    events = _object_list(payload, "events")
    markets: list[dict[str, object]] = []
    for event in events:
        raw_markets = event.get("markets", [])
        markets.extend(_object_list(raw_markets, "event markets"))
    return markets


def _select_polymarket_market(
    markets: list[dict[str, object]],
    opportunity_title: str,
    expected_condition_id: str | None = None,
) -> dict[str, object]:
    if expected_condition_id is not None:
        matches = [
            market
            for market in markets
            if _optional_text(market, "conditionId", "condition_id")
            == expected_condition_id
        ]
        if len(matches) != 1:
            raise ValueError("Polymarket condition ID must resolve to one market")
        return matches[0]
    if len(markets) == 1:
        return markets[0]
    title = opportunity_title.strip().casefold()
    matches = [
        market
        for market in markets
        if str(market.get("question", "")).strip().casefold() == title
    ]
    if len(matches) != 1:
        raise ValueError("Polymarket reference must resolve to one uniquely matched market")
    return matches[0]


def _polymarket_token(payload: dict[str, object], outcome: str) -> str:
    outcomes = _string_list(payload.get("outcomes"), "outcomes")
    token_ids = _string_list(payload.get("clobTokenIds"), "clobTokenIds")
    if len(outcomes) != len(token_ids):
        raise ValueError("Polymarket outcomes and token IDs do not align")
    normalized = outcome.strip().lower()
    matches = [token for label, token in zip(outcomes, token_ids, strict=True) if label.lower() == normalized]
    if len(matches) != 1:
        raise ValueError(f"Polymarket outcome has no unique token: {outcome}")
    return matches[0]


def _string_list(value: object, name: str) -> list[str]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise TypeError(f"Polymarket {name} must be a string list")
    return parsed


def _required_text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"Polymarket {name} must be non-empty text")
    return value


def _optional_text(payload: dict[str, object], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _decimal_field(payload: dict[str, object], *names: str) -> Decimal:
    for name in names:
        value = payload.get(name)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return Decimal(value)
    raise TypeError(f"Polymarket metadata is missing {names[0]}")


def _datetime_field(payload: dict[str, object], *names: str) -> datetime | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return datetime.fromisoformat(value).astimezone(UTC)
    return None


def _text_field(payload: dict[str, object], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _outcome(leg: OddpoolLeg) -> str:
    outcome = leg.outcome.strip().lower()
    if outcome not in {"yes", "no"}:
        raise ValueError(f"unsupported {leg.venue.value} outcome: {leg.outcome}")
    return outcome
