import asyncio
from typing import cast

import pytest

from backend.app import main
from backend.app.container import ApplicationContainer
from backend.app.core.config import TradingMode
from backend.app.db.risk_policy import PostgresRiskPolicyStore
from backend.app.main import _apply_startup_gate, _live_runtime_loop
from backend.app.services.live_runtime import LiveRuntimeService
from backend.app.services.pair_discovery import ConfiguredOddpoolPairDiscoveryService
from backend.app.services.settings import InMemoryRiskPolicyStore, RiskPolicyStore
from backend.app.services.system_control import SystemControl as SystemControlService


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
    container.pair_discovery = cast(ConfiguredOddpoolPairDiscoveryService, discovery)
    container.live_runtime = cast(LiveRuntimeService, runtime)

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


@pytest.mark.asyncio
async def test_runtime_container_uses_postgres_risk_policy_store() -> None:
    local_container = ApplicationContainer()
    runtime_container = ApplicationContainer.runtime()

    try:
        assert isinstance(local_container.risk_policies, InMemoryRiskPolicyStore)
        assert local_container.risk_policies.current is not None
        assert isinstance(runtime_container.risk_policies, PostgresRiskPolicyStore)
    finally:
        await runtime_container.close()


@pytest.mark.asyncio
async def test_lifespan_initializes_risk_policy_before_starting_runtime_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    loop_cancelled = asyncio.Event()
    container = ApplicationContainer()

    class SystemControl:
        async def load_async(self) -> None:
            events.append("system control")

    class RiskPolicies:
        current = None

        async def initialize(self) -> None:
            events.append("risk policies")

    async def apply_startup_gate(_container, _trading_mode) -> None:
        assert events == ["system control", "risk policies"]
        events.append("startup gate")

    async def live_runtime_loop(_container, *, poll_seconds: float) -> None:
        assert poll_seconds > 0
        assert events == ["system control", "risk policies", "startup gate"]
        events.append("runtime loop")
        try:
            await asyncio.Event().wait()
        finally:
            loop_cancelled.set()

    container.system_control = cast(SystemControlService, SystemControl())
    container.risk_policies = cast(RiskPolicyStore, RiskPolicies())
    container.live_runtime = cast(LiveRuntimeService, object())
    monkeypatch.setattr(
        main.ApplicationContainer,
        "runtime",
        classmethod(lambda _cls: container),
    )
    monkeypatch.setattr(main, "_apply_startup_gate", apply_startup_gate)
    monkeypatch.setattr(main, "_live_runtime_loop", live_runtime_loop)

    app = main.create_app()
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert events == [
            "system control",
            "risk policies",
            "startup gate",
            "runtime loop",
        ]

    assert loop_cancelled.is_set()


@pytest.mark.asyncio
async def test_lifespan_does_not_start_runtime_loop_when_risk_policy_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    container = ApplicationContainer()

    class SystemControl:
        async def load_async(self) -> None:
            events.append("system control")

    class RiskPolicies:
        current = None

        async def initialize(self) -> None:
            events.append("risk policies")
            raise RuntimeError("risk policy database unavailable")

    async def live_runtime_loop(_container, *, poll_seconds: float) -> None:
        events.append("runtime loop")

    async def apply_startup_gate(_container, _trading_mode) -> None:
        events.append("startup gate")

    container.system_control = cast(SystemControlService, SystemControl())
    container.risk_policies = cast(RiskPolicyStore, RiskPolicies())
    container.live_runtime = cast(LiveRuntimeService, object())
    monkeypatch.setattr(
        main.ApplicationContainer,
        "runtime",
        classmethod(lambda _cls: container),
    )
    monkeypatch.setattr(main, "_apply_startup_gate", apply_startup_gate)
    monkeypatch.setattr(main, "_live_runtime_loop", live_runtime_loop)

    app = main.create_app()

    with pytest.raises(RuntimeError, match="risk policy database unavailable"):
        async with app.router.lifespan_context(app):
            pass

    assert events == ["system control", "risk policies"]
