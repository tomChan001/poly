from typing import Annotated

from fastapi import APIRouter, Depends

from backend.app.api.dependencies import get_container
from backend.app.container import ApplicationContainer
from backend.app.core.security import require_authenticated
from backend.app.services.runtime_status import RuntimeStatusView

router = APIRouter(
    prefix="/api/runtime",
    tags=["runtime"],
    dependencies=[Depends(require_authenticated)],
)


@router.get("")
async def get_runtime_status(
    container: Annotated[ApplicationContainer, Depends(get_container)],
) -> RuntimeStatusView:
    return await container.runtime_status.view()
