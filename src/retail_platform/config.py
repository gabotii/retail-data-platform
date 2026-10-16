"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SimulationSettings(BaseSettings):
    """Parameters of the synthetic retail world.

    The CSV generator and the demo inventory API must share these values so that
    both "source systems" describe the same stores, products and days.
    """

    model_config = SettingsConfigDict(env_prefix="SIM_", env_file=".env", extra="ignore")

    seed: int = 42
    start_date: date = date(2026, 7, 1)
    days: int = Field(default=60, ge=1, le=730)
    traffic_scale: float = Field(default=0.5, gt=0, le=5)
    dirty_rate: float = Field(default=0.01, ge=0, le=0.5)

    @property
    def end_date(self) -> date:
        return date.fromordinal(self.start_date.toordinal() + self.days - 1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://retail:retail_dev_pw@127.0.0.1:5432/retail"
    data_dir: Path = Path("data")

    demo_api_url: str = "http://127.0.0.1:8001"
    demo_api_token: SecretStr = SecretStr("demo-token")
    demo_api_failure_rate: float = Field(default=0.0, ge=0, le=0.9)
    http_timeout_seconds: float = Field(default=10.0, gt=0)
    http_max_retries: int = Field(default=4, ge=0, le=10)
    http_backoff_seconds: float = Field(default=0.5, ge=0)
    api_page_size: int = Field(default=1000, ge=1, le=5000)

    quality_max_reject_ratio: float = Field(default=0.05, ge=0, le=1)

    api_key: SecretStr | None = None
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    @property
    def landing_dir(self) -> Path:
        return self.data_dir / "landing"


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_simulation_settings() -> SimulationSettings:
    return SimulationSettings()
