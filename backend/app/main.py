from fastapi import FastAPI

from backend.app.api.routes.mappings import router as mappings_router
from backend.app.container import ApplicationContainer
from backend.app.core.config import settings


def create_app(container: ApplicationContainer | None = None) -> FastAPI:
    application = FastAPI(title="Cross-Market Control Plane")
    application.state.container = container or ApplicationContainer()
    application.include_router(mappings_router)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "trading_mode": settings.trading_mode.value}

    return application


app = create_app()
