from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.app.api.dependencies import get_container
from backend.app.container import ApplicationContainer
from backend.app.core.security import (
    Principal,
    Role,
    require_authenticated,
    require_role,
)
from backend.app.services.settings import RiskPolicy, RiskPolicyInput

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
    dependencies=[Depends(require_authenticated)],
)

type Ratio = Annotated[Decimal, Field(ge=0, le=1)]
type PositiveDecimal = Annotated[Decimal, Field(gt=0)]
type NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]


class RiskPolicyRequest(BaseModel):
    minimum_roi: Ratio
    maximum_settlement_days: Annotated[int, Field(gt=0)]
    maximum_book_age_seconds: PositiveDecimal
    per_trade_limit: PositiveDecimal
    per_event_limit: PositiveDecimal
    portfolio_limit: PositiveDecimal
    explicit_cost: NonNegativeDecimal
    risk_buffer: NonNegativeDecimal
    maximum_unhedged_seconds: PositiveDecimal
    maximum_unhedged_loss: NonNegativeDecimal


@router.get("/risk")
def get_risk_policy(
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> RiskPolicy:
    policy = container.risk_policies.current
    if policy is None:  # pragma: no cover - the container always seeds a safe policy
        raise RuntimeError("risk policy has not been initialized")
    return policy


@router.put("/risk")
def update_risk_policy(
    payload: RiskPolicyRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
    _principal: Annotated[Principal, Depends(require_role(Role.OPERATOR))],
) -> RiskPolicy:
    policy_input = RiskPolicyInput(**payload.model_dump())
    return container.risk_policies.create(policy_input)
