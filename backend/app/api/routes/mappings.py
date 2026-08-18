from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.app.api.dependencies import get_container
from backend.app.container import ApplicationContainer
from backend.app.domain.enums import MappingStatus

router = APIRouter(prefix="/api/mappings", tags=["mappings"])


class TruthTableRow(BaseModel):
    kalshi: Decimal
    polymarket: Decimal


class MappingReviewRequest(BaseModel):
    status: MappingStatus
    reviewer: str
    checklist: dict[str, bool]
    truth_table: list[TruthTableRow]
    notes: str = ""


class MappingReviewResponse(BaseModel):
    mapping_id: UUID
    status: MappingStatus
    reviewer: str


@router.post("/{mapping_id}/review", response_model=MappingReviewResponse)
def review_mapping(
    mapping_id: UUID,
    payload: MappingReviewRequest,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> MappingReviewResponse:
    try:
        review = container.mappings.review(
            mapping_id,
            payload.status,
            payload.reviewer,
            payload.checklist,
            [row.model_dump() for row in payload.truth_table],
            payload.notes,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="mapping not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return MappingReviewResponse(
        mapping_id=review.mapping_id,
        status=review.status,
        reviewer=review.reviewer,
    )
