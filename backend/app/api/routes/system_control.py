from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from backend.app.api.dependencies import get_container
from backend.app.container import ApplicationContainer
from backend.app.core.security import (
    Principal,
    Role,
    require_authenticated,
    require_role,
)

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
async def set_opening_control(
    request: OpeningControlRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
    principal: Annotated[Principal, Depends(require_role(Role.OPERATOR))],
) -> dict[str, object]:
    # This database switch never cancels or closes existing positions. Those
    # actions require the evidence-driven partially-hedged runbook.
    state = await container.system_control.set_opening_async(
        request.enabled,
        request.reason,
        changed_by=principal.subject,
    )
    return {
        "opening_enabled": state.opening_enabled,
        "reason": state.reason,
        "version": state.version,
    }
