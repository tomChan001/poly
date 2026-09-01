from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.domain.enums import Venue


class OddpoolLeg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    venue: Venue
    outcome: Literal["yes", "no"]
    market_ref: str
    market_url: str | None = None
    display_price: str
    source_condition_id: str | None = None
    source_token_id: str | None = None


class OddpoolOpportunity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    outcome: str
    updated_at: datetime
    resolves_at: datetime | None = None
    gross_spread: str
    estimated_fees: str
    legs: list[OddpoolLeg] = Field(min_length=2)

    @model_validator(mode="after")
    def require_one_leg_per_supported_venue(self) -> "OddpoolOpportunity":
        venues = {leg.venue for leg in self.legs}
        if venues != {Venue.KALSHI, Venue.POLYMARKET}:
            raise ValueError("opportunity must contain one Kalshi and one Polymarket leg")
        return self


class OddpoolOfficialVenue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market_ticker: str | None = None
    condition_id: str | None = None
    yes_token_id: str | None = None
    no_token_id: str | None = None
    yes_ask: Decimal | None = None
    no_ask: Decimal | None = None


class OddpoolArbitrageRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: int | str
    event_title: str
    polymarket_event_slug: str
    outcome_key: str
    label: str
    timestamp: datetime
    resolution_time: datetime | None = None
    kalshi: OddpoolOfficialVenue
    polymarket: OddpoolOfficialVenue
    buy_yes_market: str
    buy_no_market: str
    gross_cents: Decimal
    fee_cents: Decimal

    @field_validator("timestamp", "resolution_time")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def to_opportunity(self) -> OddpoolOpportunity | None:
        sides: dict[str, Literal["yes", "no"]] = {
            self.buy_yes_market.casefold(): "yes",
            self.buy_no_market.casefold(): "no",
        }
        if set(sides) != {"kalshi", "polymarket"}:
            return None
        if not self.kalshi.market_ticker:
            raise ValueError("Kalshi market ticker is missing")
        if not self.polymarket.condition_id:
            raise ValueError("Polymarket condition ID is missing")

        kalshi_side = sides["kalshi"]
        polymarket_side = sides["polymarket"]
        kalshi_price = getattr(self.kalshi, f"{kalshi_side}_ask")
        polymarket_price = getattr(self.polymarket, f"{polymarket_side}_ask")
        polymarket_token = getattr(self.polymarket, f"{polymarket_side}_token_id")
        if kalshi_price is None or polymarket_price is None or not polymarket_token:
            raise ValueError("selected Oddpool leg is incomplete")

        return OddpoolOpportunity(
            id=f"oddpool:{self.event_id}:{self.outcome_key}",
            title=self.event_title,
            outcome=self.label,
            updated_at=self.timestamp,
            resolves_at=self.resolution_time,
            gross_spread=str(self.gross_cents / Decimal(100)),
            estimated_fees=str(self.fee_cents / Decimal(100)),
            legs=[
                OddpoolLeg(
                    venue=Venue.KALSHI,
                    outcome=kalshi_side,
                    market_ref=self.kalshi.market_ticker,
                    market_url=(
                        f"https://kalshi.com/markets/{self.kalshi.market_ticker}"
                    ),
                    display_price=str(kalshi_price),
                ),
                OddpoolLeg(
                    venue=Venue.POLYMARKET,
                    outcome=polymarket_side,
                    market_ref=self.polymarket_event_slug,
                    market_url=(
                        f"https://polymarket.com/event/{self.polymarket_event_slug}"
                    ),
                    display_price=str(polymarket_price),
                    source_condition_id=self.polymarket.condition_id,
                    source_token_id=polymarket_token,
                ),
            ],
        )


class OddpoolResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    opportunities: list[OddpoolOpportunity]
    errors: tuple[str, ...] = ()

