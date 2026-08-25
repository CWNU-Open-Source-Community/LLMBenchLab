"""Model adapter public API and provider registry."""

from __future__ import annotations

from typing import Any

from .base import AdapterError, GenerationConfig, Message, ModelAdapter, ModelGenerationResult
from .mock import MockModelAdapter
from .openai_compatible import OpenAICompatibleAdapter, sanitize_error_message


def build_adapter(provider_type: str, **kwargs: Any) -> ModelAdapter:
    """Construct an adapter from a persisted provider type and model fields."""

    normalized = provider_type.strip().lower().replace("-", "_")
    if normalized == "mock":
        return MockModelAdapter()
    if normalized in {"openai", "openai_compatible"}:
        options = dict(kwargs)
        if "remote_model_name" not in options and "model_name" in options:
            options["remote_model_name"] = options.pop("model_name")
        aliases = {
            "connect_timeout": "connect_timeout_seconds",
            "read_timeout": "read_timeout_seconds",
            "write_timeout": "write_timeout_seconds",
            "pool_timeout": "pool_timeout_seconds",
            "retry_count": "max_retries",
            "backoff_base_seconds": "retry_backoff_base_seconds",
            "backoff_cap_seconds": "retry_backoff_cap_seconds",
        }
        for old_name, new_name in aliases.items():
            if old_name in options and new_name not in options:
                options[new_name] = options.pop(old_name)
        allowed = {
            "base_url",
            "remote_model_name",
            "api_key_env",
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "write_timeout_seconds",
            "pool_timeout_seconds",
            "max_retries",
            "retry_backoff_base_seconds",
            "retry_backoff_cap_seconds",
            "client",
            "sleep",
        }
        required = {"base_url", "remote_model_name", "api_key_env"}
        adapter_options = {
            key: value
            for key, value in options.items()
            if key in allowed and (value is not None or key in required)
        }
        return OpenAICompatibleAdapter(**adapter_options)
    raise ValueError(f"Unsupported provider_type: {provider_type!r}")


get_adapter = build_adapter
OpenAICompatibleModelAdapter = OpenAICompatibleAdapter

__all__ = [
    "AdapterError",
    "GenerationConfig",
    "Message",
    "MockModelAdapter",
    "ModelAdapter",
    "ModelGenerationResult",
    "OpenAICompatibleAdapter",
    "OpenAICompatibleModelAdapter",
    "build_adapter",
    "get_adapter",
    "sanitize_error_message",
]
