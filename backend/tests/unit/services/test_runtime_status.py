import pytest

from backend.app.services.integration_config import RuntimeReadiness
from backend.app.services.runtime_status import RuntimeStatusService
from backend.app.services.system_control import SystemControl


class FakeIntegrations:
    def __init__(self, readiness: RuntimeReadiness) -> None:
        self._readiness = readiness

    async def readiness(self) -> RuntimeReadiness:
        return self._readiness


@pytest.mark.asyncio
@pytest.mark.parametrize("opening_enabled", [False, True])
async def test_view_keeps_ready_runtime_running_when_opening_permission_changes(
    opening_enabled: bool,
) -> None:
    control = SystemControl(opening_enabled=opening_enabled)
    status = RuntimeStatusService(
        FakeIntegrations(RuntimeReadiness(ready=True, missing=())),  # type: ignore[arg-type]
        control,
    )
    status.running = True

    view = await status.view()

    assert view.ready is True
    assert view.running is True
    assert view.opening_enabled is opening_enabled
    assert view.missing_providers == ()
    assert view.last_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("opening_enabled", [False, True])
async def test_view_reports_unready_integrations_independently_of_opening_permission(
    opening_enabled: bool,
) -> None:
    control = SystemControl(opening_enabled=opening_enabled)
    status = RuntimeStatusService(
        FakeIntegrations(RuntimeReadiness(ready=False, missing=("kalshi",))),  # type: ignore[arg-type]
        control,
    )
    status.running = True
    status.record_cycle(error="previous cycle failed")

    view = await status.view()

    assert view.ready is False
    assert view.running is True
    assert view.opening_enabled is opening_enabled
    assert view.missing_providers == ("kalshi",)
    assert view.last_error == "previous cycle failed"


@pytest.mark.asyncio
async def test_successful_cycle_clears_previous_error_without_changing_opening_permission() -> None:
    control = SystemControl(opening_enabled=False)
    status = RuntimeStatusService(
        FakeIntegrations(RuntimeReadiness(ready=True, missing=())),  # type: ignore[arg-type]
        control,
    )
    status.record_cycle(error="temporary venue failure")

    status.record_cycle(executions=2)
    view = await status.view()

    assert view.last_error is None
    assert view.executions_started == 2
    assert view.opening_enabled is False
