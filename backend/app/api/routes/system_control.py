from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from backend.app.api.dependencies import get_container
from backend.app.container import ApplicationContainer
from backend.app.core.config import TradingMode, settings
from backend.app.core.security import (
    Principal,
    Role,
    require_authenticated,
    require_role,
)
from backend.app.services.automation_gate import AutomationStage

router = APIRouter(
    prefix="/api/system-control",
    tags=["system-control"],
    dependencies=[Depends(require_authenticated)],
)


class OpeningControlRequest(BaseModel):
    enabled: bool
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped


@router.put("/opening")
def set_opening_control(
    request: OpeningControlRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
    _principal: Annotated[Principal, Depends(require_role(Role.OPERATOR))],
) -> dict[str, object]:
    # This database switch never cancels or closes existing positions. Those
    # actions require the evidence-driven partially-hedged runbook.
    if request.enabled:
        if settings.trading_mode is not TradingMode.LIMITED_AUTO:
            raise HTTPException(
                status_code=409,
                detail="deployment TRADING_MODE is not limited_auto",
            )
        evidence = container.automation_evidence
        if evidence is None:
            raise HTTPException(status_code=409, detail="automation evidence is missing")

        policy = container.risk_policies.current
        if policy is None:  # pragma: no cover - the container seeds a policy
            raise HTTPException(status_code=409, detail="risk policy is missing")
        within_canary_caps = (
            policy.per_trade_limit <= Decimal(10)
            and policy.per_event_limit <= Decimal(25)
            and policy.portfolio_limit <= Decimal(100)
        )
        stage = (
            AutomationStage.CANARY_AUTO
            if within_canary_caps
            else AutomationStage.LIMITED_AUTO
        )
        decision = container.automation_gate.evaluate(stage, evidence)
        if not decision.allowed:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "automation gate rejected opening",
                    "reasons": decision.reasons,
                },
            )

    container.system_control.set_opening(request.enabled, request.reason)
    return {
        "opening_enabled": container.system_control.opening_enabled,
        "reason": container.system_control.reason,
    }
