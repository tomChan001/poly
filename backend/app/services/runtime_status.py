from dataclasses import dataclass
from datetime import UTC, datetime

from backend.app.services.integration_config import IntegrationConfigService
from backend.app.services.system_control import SystemControl


@dataclass(frozen=True, slots=True)
class RuntimeStatusView:
    ready: bool
    running: bool
    opening_enabled: bool
    missing_providers: tuple[str, ...]
    last_cycle_at: datetime | None
    last_error: str | None
    executions_started: int


class RuntimeStatusService:
    def __init__(
        self,
        integrations: IntegrationConfigService,
        system_control: SystemControl,
    ) -> None:
        self._integrations = integrations
        self._system_control = system_control
        self.running = False
        self.last_cycle_at: datetime | None = None
        self.last_error: str | None = None
        self.executions_started = 0

    async def view(self) -> RuntimeStatusView:
        readiness = await self._integrations.readiness()
        return RuntimeStatusView(
            ready=readiness.ready,
            running=self.running,
            opening_enabled=self._system_control.opening_enabled,
            missing_providers=readiness.missing,
            last_cycle_at=self.last_cycle_at,
            last_error=self.last_error,
            executions_started=self.executions_started,
        )

    def record_cycle(self, *, error: str | None = None, executions: int = 0) -> None:
        self.last_cycle_at = datetime.now(UTC)
        self.last_error = error
        self.executions_started += executions
