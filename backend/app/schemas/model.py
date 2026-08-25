"""Validated and secret-safe schemas for registered models."""

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from app.models.enums import ProviderType
from app.schemas.base import APIModel, ORMModel

ENV_VAR_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALLOWED_DEFAULT_PARAMETERS = frozenset({"temperature", "top_p", "max_tokens", "seed"})


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validate_base_url(value: str | None) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain embedded credentials")
    if parsed.fragment:
        raise ValueError("base_url must not contain a URL fragment")
    if parsed.query:
        raise ValueError("base_url must not contain query parameters")
    return normalized.rstrip("/")


def _validate_default_parameters(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    unknown = sorted(set(value) - ALLOWED_DEFAULT_PARAMETERS)
    if unknown:
        raise ValueError(
            "default_parameters only supports temperature, top_p, max_tokens, and seed"
        )

    validated = dict(value)
    for key in ("temperature", "top_p", "max_tokens"):
        if key in validated and validated[key] is None:
            raise ValueError(f"default_parameters.{key} must not be null")
    for key in ("temperature", "top_p"):
        setting = validated.get(key)
        if setting is None:
            continue
        if isinstance(setting, bool) or not isinstance(setting, (int, float)):
            raise ValueError(f"default_parameters.{key} must be a finite number")
        if setting != setting or setting in {float("inf"), float("-inf")}:
            raise ValueError(f"default_parameters.{key} must be a finite number")
    temperature = validated.get("temperature")
    if temperature is not None and not 0 <= temperature <= 2:
        raise ValueError("default_parameters.temperature must be between 0 and 2")
    top_p = validated.get("top_p")
    if top_p is not None and not 0 < top_p <= 1:
        raise ValueError("default_parameters.top_p must be greater than 0 and at most 1")

    max_tokens = validated.get("max_tokens")
    if max_tokens is not None and (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or not 1 <= max_tokens <= 32768
    ):
        raise ValueError("default_parameters.max_tokens must be an integer from 1 to 32768")
    seed = validated.get("seed")
    if seed is not None and (
        isinstance(seed, bool) or not isinstance(seed, int) or not -(2**31) <= seed <= 2**31 - 1
    ):
        raise ValueError("default_parameters.seed must be a signed 32-bit integer or null")
    return validated


class ModelFields(APIModel):
    """Fields shared by create validation and reconstructed PATCH payloads."""

    name: str = Field(min_length=1, max_length=160)
    provider_type: ProviderType
    base_url: str | None = Field(default=None, max_length=2048)
    remote_model_name: str | None = Field(default=None, max_length=256)
    api_key_env: str | None = Field(default=None, max_length=128)
    enabled: bool = True
    input_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    default_parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return _validate_base_url(value)

    @field_validator("remote_model_name", "api_key_env")
    @classmethod
    def normalize_optional_fields(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str | None) -> str | None:
        if value is not None and not ENV_VAR_PATTERN.fullmatch(value):
            raise ValueError("api_key_env must be an environment variable name, not a key value")
        return value

    @field_validator("default_parameters")
    @classmethod
    def validate_default_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_default_parameters(value) or {}

    @model_validator(mode="after")
    def validate_provider_requirements(self) -> "ModelFields":
        if self.provider_type == ProviderType.MOCK:
            remote_fields = [
                field
                for field in ("base_url", "remote_model_name", "api_key_env")
                if getattr(self, field) is not None
            ]
            if remote_fields:
                raise ValueError("mock requires empty " + ", ".join(remote_fields))
            if self.input_price_per_million is None:
                self.input_price_per_million = 0
            if self.output_price_per_million is None:
                self.output_price_per_million = 0
        if self.provider_type == ProviderType.OPENAI_COMPATIBLE:
            missing = [
                field
                for field in ("base_url", "remote_model_name", "api_key_env")
                if getattr(self, field) is None
            ]
            if missing:
                raise ValueError("openai_compatible requires " + ", ".join(missing))
        return self


class ModelCreate(ModelFields):
    """Payload accepted when registering a model."""


class ModelUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    provider_type: ProviderType | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    remote_model_name: str | None = Field(default=None, max_length=256)
    api_key_env: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None
    input_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    default_parameters: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return _validate_base_url(value)

    @field_validator("remote_model_name", "api_key_env")
    @classmethod
    def normalize_optional_fields(cls, value: str | None) -> str | None:
        return _normalize_optional_text(value)

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str | None) -> str | None:
        if value is not None and not ENV_VAR_PATTERN.fullmatch(value):
            raise ValueError("api_key_env must be an environment variable name, not a key value")
        return value

    @field_validator("default_parameters")
    @classmethod
    def validate_default_parameters(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_default_parameters(value)


class ModelRead(ORMModel):
    """Public model response. It never resolves or includes an API key value."""

    id: str
    name: str
    provider_type: ProviderType
    base_url: str | None
    remote_model_name: str | None
    api_key_env: str | None
    enabled: bool
    input_price_per_million: float | None
    output_price_per_million: float | None
    default_parameters: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ModelList(ORMModel):
    items: list[ModelRead]
    total: int
    offset: int
    limit: int
