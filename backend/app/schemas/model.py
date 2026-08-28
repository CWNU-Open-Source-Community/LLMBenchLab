"""Validated and secret-safe schemas for registered models."""

import ipaddress
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from app.core.constants import MAX_GENERATION_TOKENS
from app.models.enums import CredentialSource, ProviderType
from app.schemas.base import APIModel, ORMModel

ENV_VAR_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALLOWED_DEFAULT_PARAMETERS = frozenset({"temperature", "top_p", "max_tokens", "seed"})
API_KEY_MIN_BYTES = 8
API_KEY_MAX_BYTES = 8192


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validate_api_key_value(value: SecretStr | None) -> SecretStr | None:
    if value is None:
        return None
    raw = value.get_secret_value()
    if not raw:
        raise ValueError("api_key must not be blank")
    if raw != raw.strip():
        raise ValueError("api_key must not contain leading or trailing whitespace")
    if any(not 0x21 <= ord(character) <= 0x7E for character in raw):
        raise ValueError("api_key must contain only visible ASCII characters")
    byte_length = len(raw.encode("ascii"))
    if byte_length < API_KEY_MIN_BYTES:
        raise ValueError(f"api_key must contain at least {API_KEY_MIN_BYTES} bytes")
    if byte_length > API_KEY_MAX_BYTES:
        raise ValueError(f"api_key must contain at most {API_KEY_MAX_BYTES} bytes")
    return SecretStr(raw)


def _contains_secret(value: Any, secret: str) -> bool:
    if not secret:
        return False
    if isinstance(value, str):
        return secret in value
    if isinstance(value, Enum):
        return _contains_secret(value.value, secret)
    if isinstance(value, Mapping):
        return any(
            _contains_secret(key, secret) or _contains_secret(item, secret)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_secret(item, secret) for item in value)
    if value is None:
        return secret in "null"
    if isinstance(value, bool):
        return secret in ("true" if value else "false")
    if isinstance(value, (int, float)):
        return secret in str(value)
    return False


def _validate_base_url(value: str | None) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    try:
        parsed = urlsplit(normalized)
        # ``hostname`` and ``port`` are lazy properties and either may reject a
        # malformed authority. Touch both at the HTTP validation boundary.
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise ValueError("base_url must be an absolute HTTP(S) URL") from None
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain embedded credentials")
    if parsed.fragment:
        raise ValueError("base_url must not contain a URL fragment")
    if parsed.query:
        raise ValueError("base_url must not contain query parameters")
    if parsed.scheme == "http" and not _is_loopback_host(hostname):
        raise ValueError("plain HTTP base_url is allowed only for loopback hosts")
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
    for key in ("temperature", "top_p"):
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
        or not 1 <= max_tokens <= MAX_GENERATION_TOKENS
    ):
        raise ValueError(
            "default_parameters.max_tokens must be null or an integer from "
            f"1 to {MAX_GENERATION_TOKENS}"
        )
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
    api_key: SecretStr | None = Field(
        default=None,
        repr=False,
        json_schema_extra={"writeOnly": True},
    )
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

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        return _validate_api_key_value(value)

    @field_validator("default_parameters")
    @classmethod
    def validate_default_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_default_parameters(value) or {}

    @model_validator(mode="after")
    def validate_provider_requirements(self) -> "ModelFields":
        if self.api_key is not None:
            secret = self.api_key.get_secret_value()
            if model_public_values_contain_secret(
                self,
                credential_source=CredentialSource.STORED,
                secret=secret,
            ):
                raise ValueError("api_key must not be duplicated in public model fields")
        if self.provider_type == ProviderType.MOCK:
            remote_fields = [
                field
                for field in ("base_url", "remote_model_name", "api_key_env", "api_key")
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
                field for field in ("base_url", "remote_model_name") if getattr(self, field) is None
            ]
            if self.api_key is None and self.api_key_env is None:
                missing.append("api_key or api_key_env")
            if missing:
                raise ValueError("openai_compatible requires " + ", ".join(missing))
            if self.api_key is not None and self.api_key_env is not None:
                raise ValueError("openai_compatible accepts api_key or api_key_env, not both")
        return self


class ModelCreate(ModelFields):
    """Payload accepted when registering a model."""


class ModelUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    provider_type: ProviderType | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    remote_model_name: str | None = Field(default=None, max_length=256)
    api_key_env: str | None = Field(default=None, max_length=128)
    api_key: SecretStr | None = Field(
        default=None,
        repr=False,
        json_schema_extra={"writeOnly": True},
    )
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

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        return _validate_api_key_value(value)

    @field_validator("default_parameters")
    @classmethod
    def validate_default_parameters(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_default_parameters(value)

    @model_validator(mode="after")
    def reject_explicit_empty_api_key(self) -> "ModelUpdate":
        if "api_key" in self.model_fields_set and self.api_key is None:
            raise ValueError("api_key must be omitted to preserve an existing credential")
        return self


class ModelRead(ORMModel):
    """Public model response. It never resolves or includes an API key value."""

    id: str
    name: str
    provider_type: ProviderType
    base_url: str | None
    remote_model_name: str | None
    api_key_env: str | None
    credential_source: CredentialSource
    has_api_key: bool
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


def _snapshot_price(value: object) -> str | None:
    """Render prices exactly as the persisted Numeric(20, 8) snapshot does."""

    if value is None:
        return None
    try:
        return format(Decimal(str(value)).quantize(Decimal("0.00000001")), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def model_run_snapshot_values(
    model: Any,
    *,
    credential_source: CredentialSource | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Build the one canonical public Model projection frozen into every Run."""

    provider_type = model.provider_type
    source = credential_source if credential_source is not None else model.credential_source
    resolved_id = model_id if model_id is not None else getattr(model, "id", None)
    return {
        "id": resolved_id,
        "name": model.name,
        "remote_model_name": model.remote_model_name,
        "adapter_type": provider_type.value,
        "base_url": model.base_url,
        "credential_source": source.value,
        "api_key_env": model.api_key_env,
        "input_price_per_million": _snapshot_price(model.input_price_per_million),
        "output_price_per_million": _snapshot_price(model.output_price_per_million),
        "currency_assumption": "USD",
        "default_parameters": dict(model.default_parameters or {}),
    }


def model_public_values_contain_secret(
    model: Any,
    *,
    credential_source: CredentialSource,
    secret: str,
    model_id: str | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> bool:
    """Check the exact Model response and canonical Run-model projection."""

    resolved_id = model_id if model_id is not None else getattr(model, "id", None)
    resolved_created_at = (
        created_at if created_at is not None else getattr(model, "created_at", None)
    )
    resolved_updated_at = (
        updated_at if updated_at is not None else getattr(model, "updated_at", None)
    )
    model_read_values: dict[str, Any] = {
        "id": resolved_id,
        "name": model.name,
        "provider_type": model.provider_type,
        "base_url": model.base_url,
        "remote_model_name": model.remote_model_name,
        "api_key_env": model.api_key_env,
        "credential_source": credential_source,
        "has_api_key": credential_source == CredentialSource.STORED,
        "enabled": model.enabled,
        "input_price_per_million": model.input_price_per_million,
        "output_price_per_million": model.output_price_per_million,
        "default_parameters": dict(model.default_parameters or {}),
        "created_at": resolved_created_at,
        "updated_at": resolved_updated_at,
    }
    if (
        resolved_id is not None
        and resolved_created_at is not None
        and resolved_updated_at is not None
    ):
        model_read_values = ModelRead.model_validate(model_read_values).model_dump(mode="json")

    public_surfaces = {
        "model_read": model_read_values,
        "run_model_snapshot": model_run_snapshot_values(
            model,
            credential_source=credential_source,
            model_id=resolved_id,
        ),
    }
    return _contains_secret(public_surfaces, secret)
