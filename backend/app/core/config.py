"""Typed environment configuration for the local-first MVP."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import AliasChoices, Field, field_validator, model_validator
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
    credential_keys_file: Path | None = Field(
        default=Path(__file__).resolve().parents[3] / ".secrets" / "credential-keys.json",
        validation_alias=AliasChoices("LLMBENCHLAB_CREDENTIAL_KEYS_FILE"),
    )
    database_url: str = Field(
        default="sqlite:///./data/llmbenchlab.db",
        validation_alias=AliasChoices("LLMBENCHLAB_DATABASE_URL", "DATABASE_URL"),
    )
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    readiness_database_timeout_seconds: float = Field(default=5.0, gt=0, le=30.0)
    redis_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLMBENCHLAB_REDIS_URL", "REDIS_URL"),
    )
    task_stream: str = Field(default="llmbenchlab:runs:v1", min_length=1, max_length=128)
    task_consumer_group: str = Field(default="llmbenchlab-workers-v1", min_length=1, max_length=128)
    task_stream_max_length: int = Field(default=10_000, ge=100, le=10_000_000)
    redis_max_connections: int = Field(default=10, ge=1, le=100)
    redis_publish_timeout_seconds: float = Field(default=1.0, gt=0.0, le=10.0)
    redis_operation_timeout_seconds: float = Field(default=2.0, gt=0.0, le=30.0)
    worker_lease_seconds: float = Field(default=30.0, ge=3.0, le=3600.0)
    worker_heartbeat_seconds: float = Field(default=10.0, ge=1.0, le=1200.0)
    worker_poll_seconds: float = Field(default=1.0, ge=0.05, le=60.0)
    worker_max_attempts: int = Field(default=3, ge=1, le=20)
    worker_retry_backoff_base_seconds: float = Field(default=1.0, ge=0.0, le=3600.0)
    worker_retry_backoff_cap_seconds: float = Field(default=30.0, ge=0.0, le=86_400.0)
    worker_shutdown_grace_seconds: float = Field(default=30.0, ge=0.0, le=3600.0)
    mock_generation_delay_seconds: float = Field(default=0.0, ge=0.0, le=5.0)
    redis_block_milliseconds: int = Field(default=1000, ge=50, le=60_000)
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"],
        validation_alias=AliasChoices(
            "LLMBENCHLAB_CORS_ORIGINS", "CORS_ORIGINS", "FRONTEND_ORIGIN"
        ),
    )
    trusted_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1"],
        validation_alias=AliasChoices("LLMBENCHLAB_TRUSTED_HOSTS", "TRUSTED_HOSTS"),
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

    @field_validator("trusted_hosts", mode="before")
    @classmethod
    def parse_trusted_hosts(cls, value: Any) -> Any:
        if isinstance(value, str):
            if value.lstrip().startswith("["):
                return json.loads(value)
            return [host.strip() for host in value.split(",") if host.strip()]
        return value

    @field_validator("trusted_hosts")
    @classmethod
    def reject_wildcard_trusted_hosts(cls, hosts: list[str]) -> list[str]:
        if not hosts or "*" in hosts:
            raise ValueError("trusted_hosts must be a nonempty explicit allowlist")
        return list(dict.fromkeys(hosts))

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("log_level must be CRITICAL, ERROR, WARNING, INFO, or DEBUG")
        return normalized

    @field_validator("redis_url", mode="before")
    @classmethod
    def normalize_optional_redis_url(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("credential_keys_file", mode="before")
    @classmethod
    def normalize_optional_credential_keys_file(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_worker_timing(self) -> Self:
        if self.worker_heartbeat_seconds * 2 > self.worker_lease_seconds:
            raise ValueError("worker_heartbeat_seconds must be at most half the lease duration")
        if self.worker_retry_backoff_base_seconds > self.worker_retry_backoff_cap_seconds:
            raise ValueError("worker retry backoff base must not exceed its cap")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
