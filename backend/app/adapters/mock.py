"""Deterministic, completely offline adapter used by tests and the demo."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.provider_attempts import ProviderAttemptStateUnknown

from .base import (
    AdapterError,
    GenerationConfig,
    Message,
    ModelAdapter,
    ModelGenerationResult,
    ProviderAttemptContext,
    ProviderAttemptController,
    ProviderAttemptDisposition,
    ProviderAttemptOutcome,
    _finish_provider_attempt,
    _finish_provider_attempt_after_cancellation,
    _mark_provider_attempt_send_started,
    _reserve_provider_attempt,
)


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdapterError(
            "mock_configuration_error",
            "Mock token counts must be non-negative integers or null.",
        )
    return value


def _validated_float(
    value: Any,
    *,
    error_message: str,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise AdapterError("mock_configuration_error", error_message)
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise AdapterError("mock_configuration_error", error_message) from exc
    if not math.isfinite(converted) or converted < 0:
        raise AdapterError("mock_configuration_error", error_message)
    if maximum is not None and converted > maximum:
        raise AdapterError("mock_configuration_error", error_message)
    return converted


def _configured_mock_error(generation_config: GenerationConfig) -> AdapterError | None:
    mock_error = generation_config.get("mock_error")
    if not mock_error:
        return None

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
    return AdapterError(error_type, error_message, retryable=retryable)


@dataclass(frozen=True)
class _PreparedMockGeneration:
    delay_seconds: float
    result: ModelGenerationResult
    simulated_error: AdapterError | None


def _prepare_mock_generation(generation_config: GenerationConfig) -> _PreparedMockGeneration:
    delay_seconds = _validated_float(
        generation_config.get("mock_generation_delay_seconds", 0.0),
        error_message="Mock generation delay must be between 0 and 5 seconds.",
        maximum=5,
    )
    latency_ms = _validated_float(
        generation_config.get("mock_latency_ms", 0.0),
        error_message="Mock latency must be a non-negative finite number.",
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
    prepared_usage = dict(raw_usage) if raw_usage is not None else None

    response = generation_config.get("mock_response", "")
    request_id = generation_config.get("mock_request_id")
    result = ModelGenerationResult(
        text="" if response is None else str(response),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        provider_request_id=None if request_id is None else str(request_id),
        raw_usage=prepared_usage,
        metadata={"adapter": "mock", "offline": True},
    )
    return _PreparedMockGeneration(
        delay_seconds=delay_seconds,
        result=result,
        simulated_error=_configured_mock_error(generation_config),
    )


class MockModelAdapter(ModelAdapter):
    """Return ``generation_config['mock_response']`` without doing any I/O."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        attempt_controller: ProviderAttemptController | None = None,
    ) -> None:
        self._sleep = sleep
        self._attempt_controller = attempt_controller

    async def generate(
        self,
        messages: Sequence[Message],
        generation_config: GenerationConfig,
        *,
        attempt_context: ProviderAttemptContext | None = None,
    ) -> ModelGenerationResult:
        del messages  # The configured response, rather than prompt text, is the contract.

        prepared = _prepare_mock_generation(generation_config)

        provider_attempt = self._provider_attempt(attempt_context)
        permit = await _reserve_provider_attempt(
            self._attempt_controller,
            attempt_context,
            provider_attempt=provider_attempt,
        )
        try:
            await _mark_provider_attempt_send_started(self._attempt_controller, permit)
        except asyncio.CancelledError as exc:
            exc.add_note("Provider send-start outcome is unknown; reconciliation is required.")
            raise
        except ProviderAttemptStateUnknown:
            raise
        except BaseException:
            await _finish_provider_attempt(
                self._attempt_controller,
                permit,
                disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
                outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
            )
            raise

        try:
            if prepared.delay_seconds:
                await self._sleep(prepared.delay_seconds)
            if prepared.simulated_error is not None:
                raise prepared.simulated_error
            result = prepared.result
        except asyncio.CancelledError as exc:
            confirmed = await _finish_provider_attempt_after_cancellation(
                self._attempt_controller,
                permit,
                disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
                outcome=ProviderAttemptOutcome.CANCELLED,
            )
            if not confirmed:
                exc.add_note(
                    "Provider attempt settlement was not confirmed; reconciliation is required."
                )
            raise
        except AdapterError:
            await _finish_provider_attempt(
                self._attempt_controller,
                permit,
                disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
                outcome=ProviderAttemptOutcome.PROVIDER_RESPONSE_ERROR,
            )
            raise
        except BaseException:
            await _finish_provider_attempt(
                self._attempt_controller,
                permit,
                disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
                outcome=ProviderAttemptOutcome.UNEXPECTED_ERROR,
            )
            raise

        usage_complete = result.input_tokens is not None and result.output_tokens is not None
        await _finish_provider_attempt(
            self._attempt_controller,
            permit,
            disposition=(
                ProviderAttemptDisposition.SETTLED_ACTUAL
                if usage_complete
                else ProviderAttemptDisposition.SETTLED_CONSERVATIVE
            ),
            outcome=(
                ProviderAttemptOutcome.SUCCEEDED
                if usage_complete
                else ProviderAttemptOutcome.USAGE_INCOMPLETE
            ),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        return result

    def _provider_attempt(self, context: ProviderAttemptContext | None) -> int:
        if self._attempt_controller is None:
            if context is not None:
                raise ValueError("attempt_context requires an attempt_controller")
            return 1
        if context is None:
            raise ValueError("attempt_context is required when attempt_controller is configured")
        provider_attempt = context.next_provider_attempt
        if (
            isinstance(provider_attempt, bool)
            or not isinstance(provider_attempt, int)
            or provider_attempt < 1
        ):
            raise ValueError("attempt_context.next_provider_attempt must be a positive integer")
        return provider_attempt
