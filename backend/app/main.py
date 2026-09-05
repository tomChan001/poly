import asyncio
import stat
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.app.api.routes.executions import router as executions_router
from backend.app.api.routes.integrations import router as integrations_router
from backend.app.api.routes.mappings import router as mappings_router
from backend.app.api.routes.opportunities import router as opportunities_router
from backend.app.api.routes.pairs import router as pairs_router
from backend.app.api.routes.runtime import router as runtime_router
from backend.app.api.routes.settings import router as settings_router
from backend.app.api.routes.system_control import router as system_control_router
from backend.app.container import ApplicationContainer
from backend.app.core.config import Settings, desktop_mode_enabled, settings
from backend.app.core.security import is_local_setup_request, is_loopback_request
from backend.app.desktop.session import DesktopSession
from backend.app.services.system_control import OpeningControlPersistenceError


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
    configured_settings: Settings | None = None,
    manage_runtime_lifespan: bool | None = None,
    allow_local_setup: bool | None = None,
    desktop_session: DesktopSession | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    owns_container = container is None
    active_settings = configured_settings or settings
    if container is not None:
        application_container = container
    elif configured_settings is None:
        application_container = ApplicationContainer.runtime()
    else:
        application_container = ApplicationContainer.runtime(
            configured_settings=active_settings
        )
    manages_runtime = (
        owns_container if manage_runtime_lifespan is None else manage_runtime_lifespan
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        runtime_task: asyncio.Task[None] | None = None
        try:
            if manages_runtime and application_container.live_runtime is not None:
                await application_container.system_control.load_async()
                await application_container.risk_policies.initialize()
                runtime_task = asyncio.create_task(
                    _live_runtime_loop(
                        application_container,
                        poll_seconds=active_settings.runtime_poll_seconds,
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
            if manages_runtime:
                await application_container.close()

    application = FastAPI(title="Cross-Market Control Plane", lifespan=lifespan)
    application.state.container = application_container
    application.state.settings = active_settings
    application.state.desktop_session = desktop_session
    application.state.allow_local_setup = (
        owns_container if allow_local_setup is None else allow_local_setup
    )

    @application.middleware("http")
    async def desktop_security_headers(request: Request, call_next):
        response = await call_next(request)
        if desktop_session is not None:
            response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            response.headers["X-Frame-Options"] = "DENY"
        return response

    application.include_router(executions_router)
    application.include_router(integrations_router)
    application.include_router(mappings_router)
    application.include_router(opportunities_router)
    application.include_router(pairs_router)
    application.include_router(runtime_router)
    application.include_router(settings_router)
    application.include_router(system_control_router)

    @application.get("/desktop/bootstrap/{token}", include_in_schema=False)
    async def desktop_bootstrap(token: str, request: Request) -> Response:
        if desktop_session is None:
            raise HTTPException(status_code=404)
        if not is_loopback_request(request):
            raise HTTPException(status_code=403, detail="local access only")
        if not desktop_session.has_trusted_authority(request):
            raise HTTPException(status_code=403, detail="local access only")
        capability = desktop_session.exchange(token)
        if capability is None:
            raise HTTPException(status_code=403, detail="invalid desktop bootstrap")
        return RedirectResponse(
            f"/#poly_session={capability}",
            status_code=303,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "frame-ancestors 'none'",
            },
        )

    @application.get("/health")
    async def health(request: Request) -> dict[str, object]:
        if desktop_session is not None:
            if not is_local_setup_request(request):
                raise HTTPException(status_code=403, detail="local access only")
            if not desktop_session.is_authorized(request):
                raise HTTPException(status_code=403, detail="desktop session required")
        # The UI must display the same two switches used by order execution;
        # returning both prevents a hard-coded banner from drifting from reality.
        try:
            state = await application_container.system_control.refresh_async()
        except OpeningControlPersistenceError as exc:
            raise HTTPException(
                status_code=503,
                detail="opening control persistence unavailable",
            ) from exc
        return {
            "status": "ok",
            "opening_enabled": state.opening_enabled,
            "reason": state.reason,
        }

    if static_dir is not None:
        desktop_ui = StaticFiles(
            directory=static_dir,
            html=True,
            check_dir=False,
        )
        _, index_stat = desktop_ui.lookup_path("index.html")
        if index_stat is None or not stat.S_ISREG(index_stat.st_mode):
            raise ValueError("desktop static directory has no index.html")
        application.mount("/", desktop_ui, name="desktop-ui")

    return application


# Uvicorn's normal ``backend.app.main:app`` target retains its eager application,
# while the desktop runtime imports only ``create_app`` with explicit settings.
app = None if desktop_mode_enabled() else create_app()
