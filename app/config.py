from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", validate_assignment=True)

    database_url: str = "postgresql+asyncpg://mediaforge:mediaforge@postgres:5432/mediaforge"
    redis_url: str = "redis://redis:6379/0"
    storage_root: Path = Path("/data/media")
    max_upload_bytes: int = 2 * 1024**3

    llm_backend: Literal["anthropic", "fake"] = "anthropic"
    llm_model: str = "claude-sonnet-5"
    anthropic_api_key: SecretStr | None = None

    decision_backend: Literal["jev", "fake"] = "jev"
    typesafe_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
