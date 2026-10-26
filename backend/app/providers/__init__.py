"""Trusted-local provider discovery and compatibility preflight helpers."""

from .preflight import (
    CanaryResult,
    ModelDiscoveryResult,
    ProviderPreflightError,
    discover_models,
    models_url,
    run_chat_canary,
    run_provider_canary,
    select_remote_model,
)

__all__ = [
    "CanaryResult",
    "ModelDiscoveryResult",
    "ProviderPreflightError",
    "discover_models",
    "models_url",
    "run_chat_canary",
    "run_provider_canary",
    "select_remote_model",
]
