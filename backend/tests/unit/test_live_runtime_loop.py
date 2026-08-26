import asyncio

import pytest

from backend.app.container import ApplicationContainer
from backend.app.core.config import TradingMode
from backend.app.main import _apply_startup_gate, _live_runtime_loop


class FailingRuntime:
    def __init__(self, discovery: "Discovery") -> None:
        self.called = asyncio.Event()
        self.discovery = discovery

    async def run_once(self, _now) -> int:
        assert self.discovery.calls == 1
        self.called.set()
        raise RuntimeError("venue unavailable")


class Discovery:
    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self) -> None:
        self.calls += 1


@pytest.mark.asyncio
async def test_live_runtime_loop_records_cycle_errors_and_cancels_cleanly() -> None:
    container = ApplicationContainer()
    discovery = Discovery()
    runtime = FailingRuntime(discovery)
    container.pair_discovery = discovery
    container.live_runtime = runtime

    task = asyncio.create_task(_live_runtime_loop(container, poll_seconds=60))
    await asyncio.wait_for(runtime.called.wait(), timeout=1)
    await asyncio.sleep(0)

    assert container.runtime_status.running is True
    assert container.runtime_status.last_error == "venue unavailable"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert container.runtime_status.running is False


@pytest.mark.asyncio
async def test_runtime_startup_closes_opening_when_evidence_is_missing() -> None:
    container = ApplicationContainer()
    container.system_control.set_opening(True, "configured open")
    container.automation_evidence = None

    await _apply_startup_gate(container, TradingMode.LIMITED_AUTO)

    assert container.system_control.opening_enabled is False
    assert container.system_control.reason == "startup gate: automation evidence is missing"
