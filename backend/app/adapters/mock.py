"""Deterministic, completely offline adapter used by tests and the demo."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from .base import AdapterError, GenerationConfig, Message, ModelAdapter, ModelGenerationResult


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdapterError(
            "mock_configuration_error",
            "Mock token counts must be non-negative integers or null.",
        )
    return value


class MockModelAdapter(ModelAdapter):
    """Return ``generation_config['mock_response']`` without doing any I/O."""

    def __init__(self, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._sleep = sleep

    async def generate(
        self,
        messages: Sequence[Message],
        generation_config: GenerationConfig,
    ) -> ModelGenerationResult:
        del messages  # The configured response, rather than prompt text, is the contract.

        delay_value = generation_config.get("mock_generation_delay_seconds", 0.0)
        if isinstance(delay_value, bool):
            raise AdapterError(
                "mock_configuration_error",
                "Mock generation delay must be between 0 and 5 seconds.",
            )
        try:
            delay_seconds = float(delay_value)
        except (TypeError, ValueError) as exc:
            raise AdapterError(
                "mock_configuration_error",
                "Mock generation delay must be between 0 and 5 seconds.",
            ) from exc
        if not 0 <= delay_seconds <= 5 or delay_seconds != delay_seconds:
            raise AdapterError(
                "mock_configuration_error",
                "Mock generation delay must be between 0 and 5 seconds.",
            )
        if delay_seconds:
            await self._sleep(delay_seconds)

        mock_error = generation_config.get("mock_error")
        if mock_error:
            error_type = "mock_error"
            error_message = "Configured mock generation failure."
            retryable = False
            if isinstance(mock_error, str):
                error_type = mock_error
            elif isinstance(mock_error, Mapping):
                configured_type = mock_error.get("error_type", mock_error.get("type"))
                configured_message = mock_error.get("error_message", mock_error.get("message"))
                if configured_type:
                    error_type = str(configured_type)
                if configured_message:
                    error_message = str(configured_message)
                retryable = bool(mock_error.get("retryable", False))
            configured_message = generation_config.get("mock_error_message")
            if configured_message:
                error_message = str(configured_message)
            raise AdapterError(error_type, error_message, retryable=retryable)

        response = generation_config.get("mock_response", "")
        text = "" if response is None else str(response)
        latency_value = generation_config.get("mock_latency_ms", 0.0)
        if isinstance(latency_value, bool):
            raise AdapterError(
                "mock_configuration_error",
                "Mock latency must be a non-negative finite number.",
            )
        try:
            latency_ms = float(latency_value)
        except (TypeError, ValueError) as exc:
            raise AdapterError(
                "mock_configuration_error",
                "Mock latency must be a non-negative finite number.",
            ) from exc
        if latency_ms < 0 or latency_ms == float("inf") or latency_ms != latency_ms:
            raise AdapterError(
                "mock_configuration_error",
                "Mock latency must be a non-negative finite number.",
            )

        input_tokens = _optional_non_negative_int(
            generation_config.get("mock_input_tokens", generation_config.get("input_tokens"))
        )
        output_tokens = _optional_non_negative_int(
            generation_config.get("mock_output_tokens", generation_config.get("output_tokens"))
        )
        raw_usage = generation_config.get("mock_usage")
        if raw_usage is not None and not isinstance(raw_usage, Mapping):
            raise AdapterError(
                "mock_configuration_error",
                "Mock usage must be an object or null.",
            )

        request_id = generation_config.get("mock_request_id")
        return ModelGenerationResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            provider_request_id=None if request_id is None else str(request_id),
            raw_usage=dict(raw_usage) if raw_usage is not None else None,
            metadata={"adapter": "mock", "offline": True},
        )
