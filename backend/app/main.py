import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI

from backend.app.api.routes.executions import router as executions_router
from backend.app.api.routes.integrations import router as integrations_router
from backend.app.api.routes.mappings import router as mappings_router
from backend.app.api.routes.opportunities import router as opportunities_router
from backend.app.api.routes.pairs import router as pairs_router
from backend.app.api.routes.runtime import router as runtime_router
from backend.app.api.routes.settings import router as settings_router
from backend.app.api.routes.system_control import router as system_control_router
from backend.app.container import ApplicationContainer
from backend.app.core.config import TradingMode, settings
from backend.app.services.automation_gate import AutomationStage


async def _apply_startup_gate(
    container: ApplicationContainer,
    trading_mode: TradingMode,
) -> None:
    """Fail closed before any runtime cycle can reach order authorization."""
    if trading_mode is not TradingMode.LIMITED_AUTO:
        await container.system_control.disable_opening_async(
            "startup gate: deployment is not limited_auto"
        )
        return
    evidence = container.automation_evidence
    if evidence is None:
        await container.system_control.disable_opening_async(
            "startup gate: automation evidence is missing"
        )
        return
    policy = container.risk_policies.current
    if policy is None:
        await container.system_control.disable_opening_async(
            "startup gate: risk policy is missing"
        )
        return
    within_canary_caps = (
        policy.per_trade_limit <= 10
        and policy.per_event_limit <= 25
        and policy.portfolio_limit <= 100
    )
    stage = AutomationStage.CANARY_AUTO if within_canary_caps else AutomationStage.LIMITED_AUTO
    decision = container.automation_gate.evaluate(stage, evidence)
    if not decision.allowed:
        await container.system_control.disable_opening_async(
            "startup gate: " + "; ".join(decision.reasons)
        )


async def _live_runtime_loop(
    container: ApplicationContainer,
    *,
    poll_seconds: float,
) -> None:
    runtime = container.live_runtime
    if runtime is None:
        return
    container.runtime_status.running = True
    try:
        while True:
            discovery_error: str | None = None
            if container.pair_discovery is not None:
                try:
                    discovery_result = await container.pair_discovery.run_once()
                    if discovery_result.errors:
                        discovery_error = "Oddpool candidate errors: " + "; ".join(
                            discovery_result.errors
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - discovery must not stop execution
                    discovery_error = f"Oddpool discovery failed: {exc}"
            try:
                await runtime.run_once(datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - cycle errors must not stop the API
                # Error text is operational state for the local UI. Credential
                # values are never included in exception construction here.
                container.runtime_status.record_cycle(error=str(exc))
            else:
                if discovery_error is not None:
                    container.runtime_status.record_cycle(error=discovery_error)
            await asyncio.sleep(poll_seconds)
    finally:
        container.runtime_status.running = False


def create_app(
    container: ApplicationContainer | None = None,
    *,
    allow_local_setup: bool | None = None,
) -> FastAPI:
    owns_container = container is None
    application_container = container or ApplicationContainer.runtime()

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        runtime_task: asyncio.Task[None] | None = None
        try:
            if owns_container and application_container.live_runtime is not None:
                await application_container.system_control.load_async()
                await application_container.risk_policies.initialize()
                await _apply_startup_gate(application_container, settings.trading_mode)
                runtime_task = asyncio.create_task(
                    _live_runtime_loop(
                        application_container,
                        poll_seconds=settings.runtime_poll_seconds,
                    )
                )
            yield
        finally:
            if runtime_task is not None:
                runtime_task.cancel()
                try:
                    await runtime_task
                except asyncio.CancelledError:
                    pass
            if owns_container:
                await application_container.close()

    application = FastAPI(title="Cross-Market Control Plane", lifespan=lifespan)
    application.state.container = application_container
    application.state.allow_local_setup = (
        owns_container if allow_local_setup is None else allow_local_setup
    )
    application.include_router(executions_router)
    application.include_router(integrations_router)
    application.include_router(mappings_router)
    application.include_router(opportunities_router)
    application.include_router(pairs_router)
    application.include_router(runtime_router)
    application.include_router(settings_router)
    application.include_router(system_control_router)

    @application.get("/health")
    def health() -> dict[str, object]:
        # The UI must display the same two switches used by order execution;
        # returning both prevents a hard-coded banner from drifting from reality.
        return {
            "status": "ok",
            "trading_mode": settings.trading_mode.value,
            "opening_enabled": application_container.system_control.opening_enabled,
            "reason": application_container.system_control.reason,
        }

    return application


app = create_app()
