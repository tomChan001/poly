from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    opening_enabled: bool = False
    database_url: str = "postgresql+asyncpg://poly:poly@localhost:5432/poly"
    credential_service_name: str = "poly-controlled-execution"
    local_setup_enabled: bool = False
    runtime_poll_seconds: float = 5.0
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"


settings = Settings()
