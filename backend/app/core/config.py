from enum import StrEnum

from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(StrEnum):
    READ_ONLY = "read_only"
    SHADOW = "shadow"
    LIMITED_AUTO = "limited_auto"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # A missing environment variable must never enable real orders.
    trading_mode: TradingMode = TradingMode.READ_ONLY
    database_url: str = "postgresql+asyncpg://poly:poly@localhost:5432/poly"


settings = Settings()
