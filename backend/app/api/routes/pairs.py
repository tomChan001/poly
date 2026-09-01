from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.app.api.dependencies import get_container
from backend.app.container import ApplicationContainer
from backend.app.core.security import (
    Principal,
    Role,
    require_authenticated,
    require_role,
)
from backend.app.domain.enums import MappingStatus
from backend.app.services.executable_pairs import ExecutablePair

router = APIRouter(
    prefix="/api/pairs",
    tags=["pairs"],
    dependencies=[Depends(require_authenticated)],
)


class PairReviewRequest(BaseModel):
    status: MappingStatus
    checklist: dict[str, bool]
    truth_table: list[dict[str, Decimal]]
    notes: str = ""


@router.get("")
async def list_pairs(
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> list[ExecutablePair]:
    return await container.executable_pairs.list()


@router.post("/{pair_id}/review")
async def review_pair(
    pair_id: str,
    payload: PairReviewRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
    principal: Annotated[Principal, Depends(require_role(Role.REVIEWER))],
) -> ExecutablePair:
    try:
        return await container.executable_pairs.review(
            pair_id,
            status=payload.status,
            checklist=payload.checklist,
            truth_table=payload.truth_table,
            notes=payload.notes,
            reviewer=principal.subject,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="pair not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
