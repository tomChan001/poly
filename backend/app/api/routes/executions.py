from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from backend.app.api.dependencies import get_container
from backend.app.container import ApplicationContainer
from backend.app.core.security import require_authenticated
from backend.app.domain.enums import Venue
from backend.app.services.execution import ExecutionRecord

router = APIRouter(
    prefix="/api/executions",
    tags=["executions"],
    dependencies=[Depends(require_authenticated)],
)


def _view(record: ExecutionRecord) -> dict[str, object]:
    return {
        "correlation_id": record.correlation_id,
        "state": record.state.value,
        "requested_quantity": str(record.requested_quantity),
        "matched_quantity": str(record.matched_quantity),
        "unhedged_quantity": str(record.unhedged_quantity),
        "legs": {
            venue.value: {
                "client_order_id": record.legs[venue].client_order_id,
                "status": record.legs[venue].status.value,
                "filled_quantity": str(record.legs[venue].filled_quantity),
            }
            for venue in Venue
            if venue in record.legs
        },
        "transitions": [
            {
                "source": transition.source.value,
                "target": transition.target.value,
                "occurred_at": transition.occurred_at.isoformat(),
            }
            for transition in record.transitions
        ],
    }


@router.get("")
def list_executions(
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> list[dict[str, object]]:
    return [_view(record) for record in container.executions.list()]


@router.get("/{correlation_id}")
def get_execution(
    correlation_id: str,
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> dict[str, object]:
    try:
        return _view(container.executions.get(correlation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="execution not found") from exc
