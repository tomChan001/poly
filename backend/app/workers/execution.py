from collections.abc import Callable
from datetime import datetime

from backend.app.services.execution import (
    ControlledExecutionService,
    ExecutionAuthorization,
    ExecutionEvidence,
    ExecutionRecord,
)


class ExecutionWorker:
    def __init__(
        self,
        service: ControlledExecutionService,
        evidence_loader: Callable[[ExecutionAuthorization], ExecutionEvidence],
    ) -> None:
        self._service = service
        self._evidence_loader = evidence_loader

    async def process(
        self,
        authorization: ExecutionAuthorization,
        now: datetime,
    ) -> ExecutionRecord:
        # Authorization is only a short lease. Refreshing all bound versions
        # here prevents a queue delay from submitting against an old book.
        current_evidence = self._evidence_loader(authorization)
        return await self._service.execute(authorization, current_evidence, now)
