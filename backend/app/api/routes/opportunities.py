from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from backend.app.api.dependencies import get_container
from backend.app.container import ApplicationContainer
from backend.app.services.opportunities import OpportunityRecord

router = APIRouter(prefix="/api/opportunities", tags=["opportunities"])


@router.get("")
def list_opportunities(
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> list[OpportunityRecord]:
    return container.opportunities.list_ranked()


@router.get("/{opportunity_id}")
def get_opportunity(
    opportunity_id: str,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> OpportunityRecord:
    try:
        return container.opportunities.get(opportunity_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="opportunity not found") from exc
