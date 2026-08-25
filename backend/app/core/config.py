"""Typed environment configuration for the local-first MVP."""

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from ``LLMBENCHLAB_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LLMBENCHLAB_",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "LLMBenchLab"
    app_version: str = "0.1.0"
    environment: str = "development"
    debug: bool = False
    database_url: str = Field(
        default="sqlite:///./data/llmbenchlab.db",
        validation_alias=AliasChoices("LLMBENCHLAB_DATABASE_URL", "DATABASE_URL"),
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"],
        validation_alias=AliasChoices(
            "LLMBENCHLAB_CORS_ORIGINS", "CORS_ORIGINS", "FRONTEND_ORIGIN"
        ),
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("LLMBENCHLAB_LOG_LEVEL", "LOG_LEVEL"),
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> Any:
        """Accept JSON arrays or a convenient comma-separated environment value."""

        if isinstance(value, str):
            if value.lstrip().startswith("["):
                return json.loads(value)
            return [origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def reject_wildcard_cors(cls, origins: list[str]) -> list[str]:
        normalized = [origin.rstrip("/") for origin in origins]
        if "*" in normalized:
            raise ValueError("CORS origins must be explicit; wildcard origins are not allowed")
        return list(dict.fromkeys(normalized))

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("log_level must be CRITICAL, ERROR, WARNING, INFO, or DEBUG")
        return normalized


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
