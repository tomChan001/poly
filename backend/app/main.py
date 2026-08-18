from fastapi import FastAPI

from backend.app.core.config import settings

app = FastAPI(title="Cross-Market Control Plane")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "trading_mode": settings.trading_mode.value}
