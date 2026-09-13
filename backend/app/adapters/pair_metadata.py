import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlsplit

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
        polymarket_candidates = _object_list(
            _gamma_payload(polymarket_response),
            "markets",
        )
        if not polymarket_candidates:
            event_response = await self._http.get(
                f"{self._polymarket_gamma_url}/events",
                params={"slug": slug},
            )
            event_response.raise_for_status()
            polymarket_candidates = _event_markets(_gamma_payload(event_response))
        polymarket = _select_polymarket_market(
            polymarket_candidates,
            opportunity.title,
            polymarket_leg.source_condition_id,
        )
        token_id = _polymarket_token(
            polymarket,
            polymarket_leg.outcome,
            polymarket_leg.source_token_id,
        )
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
        kalshi_settlement, kalshi_latest = _kalshi_settlements(kalshi_payload)
        polymarket_settlement = _datetime_field(
            polymarket,
            "settlementDate",
            "resolutionDate",
            "endDate",
            "end_date",
        )
        kalshi_market_url = _official_market_url(
            _optional_text(kalshi_payload, "rules_url"), "kalshi.com", "/markets/"
        ) or _official_market_url(kalshi_leg.market_url, "kalshi.com", "/markets/")
        polymarket_market_url = _polymarket_market_url(polymarket, polymarket_leg)

        # Kalshi accepts whole contract counts, so the shared quantity step is
        # one contract even when Polymarket exposes finer token quantities.
        pair = ExecutablePairInput(
            title=opportunity.title,
            kalshi_market_id=kalshi.external_id,
            kalshi_outcome=_outcome(kalshi_leg),
            kalshi_rule_text=kalshi.rule_text,
            kalshi_rule_url=kalshi.rule_url,
            kalshi_market_url=kalshi_market_url,
            polymarket_market_id=token_id,
            polymarket_outcome=_outcome(polymarket_leg),
            polymarket_rule_text=polymarket_rule,
            polymarket_rule_url=polymarket_market_url,
            polymarket_market_url=polymarket_market_url,
            polymarket_resolution_source=_text_field(polymarket, "resolutionSource"),
            minimum_quantity=max(
                kalshi.minimum_quantity, polymarket_minimum, Decimal(1)
            ),
            quantity_step=Decimal(1),
            enabled=True,
            kalshi_expected_settlement_at=kalshi_settlement,
            polymarket_expected_settlement_at=polymarket_settlement,
            # A planning horizon, not a guaranteed bound: Gamma endDate is an
            # estimate and resolution disputes can delay either venue.
            worst_case_settlement_at=(
                max(
                    kalshi_settlement,
                    kalshi_latest or kalshi_settlement,
                    polymarket_settlement,
                )
                if kalshi_settlement is not None and polymarket_settlement is not None
                else None
            ),
            kalshi_category=_text_field(
                kalshi_payload, "category", "series_ticker", "seriesTicker"
            ),
            polymarket_category=_text_field(
                polymarket, "category", "seriesSlug", "groupItemTitle"
            ),
            kalshi_minimum_tick=kalshi.minimum_tick,
            polymarket_minimum_tick=_decimal_field(
                polymarket,
                "orderPriceMinTickSize",
                "minimum_tick_size",
                maximum=Decimal(1),
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


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Polymarket metadata contains invalid number: {value}")


def _gamma_payload(response: httpx.Response) -> object:
    return json.loads(
        response.text,
        parse_float=Decimal,
        parse_constant=_reject_json_constant,
    )


def _object_list(payload: object, name: str) -> list[dict[str, object]]:
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        raise TypeError(f"Polymarket {name} response must be an object list")
    return payload


def _event_markets(payload: object) -> list[dict[str, object]]:
    events = _object_list(payload, "events")
    markets: list[dict[str, object]] = []
    for event in events:
        raw_markets = event.get("markets", [])
        for market in _object_list(raw_markets, "event markets"):
            markets.append({**market, "events": [{"slug": event.get("slug")}]})
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
        raise ValueError(
            "Polymarket reference must resolve to one uniquely matched market"
        )
    return matches[0]


def _polymarket_token(
    payload: dict[str, object],
    outcome: str,
    expected_token_id: str | None = None,
) -> str:
    outcomes = _string_list(payload.get("outcomes"), "outcomes")
    token_ids = _string_list(payload.get("clobTokenIds"), "clobTokenIds")
    if len(outcomes) != len(token_ids):
        raise ValueError("Polymarket outcomes and token IDs do not align")
    if expected_token_id is not None:
        matches = [token for token in token_ids if token == expected_token_id]
        if len(matches) != 1:
            raise ValueError("Polymarket token ID must resolve to one native token")
        normalized_outcomes = [label.strip().lower() for label in outcomes]
        if set(normalized_outcomes) == {"yes", "no"}:
            selected_outcome = normalized_outcomes[token_ids.index(expected_token_id)]
            if selected_outcome != outcome.strip().lower():
                raise ValueError("Polymarket token ID does not match requested outcome")
        return matches[0]
    normalized = outcome.strip().lower()
    matches = [
        token
        for label, token in zip(outcomes, token_ids, strict=True)
        if label.strip().lower() == normalized
    ]
    if len(matches) != 1:
        raise ValueError(f"Polymarket outcome has no unique token: {outcome}")
    return matches[0]


def _string_list(value: object, name: str) -> list[str]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
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


def _decimal_field(
    payload: dict[str, object],
    *names: str,
    maximum: Decimal | None = None,
) -> Decimal:
    for name in names:
        if name not in payload:
            continue
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise TypeError(f"Polymarket {name} must be an exact decimal")
        try:
            parsed = value if isinstance(value, Decimal) else Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"Polymarket {name} is not a valid decimal") from exc
        if not parsed.is_finite():
            raise ValueError(f"Polymarket {name} must be finite")
        if parsed <= 0:
            raise ValueError(f"Polymarket {name} must be positive")
        if maximum is not None and parsed > maximum:
            raise ValueError(f"Polymarket {name} exceeds its maximum")
        return parsed
    raise TypeError(f"Polymarket metadata is missing {names[0]}")


def _datetime_field(payload: dict[str, object], *names: str) -> datetime | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value)
            return (
                parsed.replace(tzinfo=UTC)
                if parsed.tzinfo is None
                else parsed.astimezone(UTC)
            )
    return None


def _kalshi_settlements(
    payload: dict[str, object],
) -> tuple[datetime | None, datetime | None]:
    settled = _datetime_field(payload, "settlement_ts", "settlement_date")
    if settled is not None:
        return settled, settled
    expected = _datetime_field(
        payload, "expected_expiration_time", "latest_expiration_time", "expiration_time"
    )
    latest = _datetime_field(payload, "latest_expiration_time", "expiration_time")
    timer = payload.get("settlement_timer_seconds", 0)
    if isinstance(timer, bool) or not isinstance(timer, int) or timer < 0:
        raise ValueError(
            "Kalshi settlement_timer_seconds must be a non-negative integer"
        )
    delay = timedelta(seconds=timer)
    return (
        expected + delay if expected is not None else None,
        latest + delay if latest is not None else None,
    )


def _official_market_url(value: str | None, host: str, prefix: str) -> str:
    if not value or any(ord(character) < 32 for character in value) or "\\" in value:
        return ""
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {host, f"www.{host}"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or not parsed.path.startswith(prefix)
        ):
            return ""
    except ValueError:
        return ""
    return value.strip()


def _polymarket_market_url(payload: dict[str, object], leg: OddpoolLeg) -> str:
    source_url = _official_market_url(leg.market_url, "polymarket.com", "/event/")
    if source_url:
        return source_url
    market_slug = _optional_text(payload, "slug")
    events = payload.get("events")
    if isinstance(events, list) and len(events) == 1 and isinstance(events[0], dict):
        event_slug = _optional_text(events[0], "slug")
        if event_slug:
            path = quote(event_slug, safe="")
            if market_slug and market_slug != event_slug:
                path += "/" + quote(market_slug, safe="")
            return f"https://polymarket.com/event/{path}"
    return (
        f"https://polymarket.com/event/{quote(market_slug or leg.market_ref, safe='')}"
    )


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
