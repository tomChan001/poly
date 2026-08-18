from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.domain.enums import Venue


class OddpoolLeg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    venue: Venue
    outcome: str
    market_url: str
    display_price: str


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


class OddpoolResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    opportunities: list[OddpoolOpportunity]

