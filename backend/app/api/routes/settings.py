from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.app.api.dependencies import get_container
from backend.app.container import ApplicationContainer
from backend.app.services.settings import RiskPolicy, RiskPolicyInput

router = APIRouter(prefix="/api/settings", tags=["settings"])


class RiskPolicyRequest(BaseModel):
    minimum_roi: Decimal
    maximum_settlement_days: int
    maximum_book_age_seconds: Decimal
    per_trade_limit: Decimal
    per_event_limit: Decimal
    portfolio_limit: Decimal
    explicit_cost: Decimal
    risk_buffer: Decimal
    maximum_unhedged_seconds: Decimal
    maximum_unhedged_loss: Decimal


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
) -> RiskPolicy:
    policy_input = RiskPolicyInput(**payload.model_dump())
    return container.risk_policies.create(policy_input)

