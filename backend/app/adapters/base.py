"""Provider-independent model generation contracts."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from app.provider_attempts import (
    ProviderAttemptContext,
    ProviderAttemptController,
    ProviderAttemptDisposition,
    ProviderAttemptOutcome,
    ProviderAttemptPermit,
)

Message = Mapping[str, Any]
GenerationConfig = Mapping[str, Any]
_ATTEMPT_FINISH_SHIELD_TIMEOUT_SECONDS = 5.0


async def _reserve_provider_attempt(
    controller: ProviderAttemptController | None,
    context: ProviderAttemptContext | None,
    *,
    provider_attempt: int,
) -> ProviderAttemptPermit | None:
    if controller is None:
        return None
    assert context is not None
    return await controller.reserve(context, provider_attempt=provider_attempt)


async def _mark_provider_attempt_send_started(
    controller: ProviderAttemptController | None,
    permit: ProviderAttemptPermit | None,
) -> None:
    if controller is not None:
        assert permit is not None
        await controller.mark_send_started(permit)


async def _finish_provider_attempt(
    controller: ProviderAttemptController | None,
    permit: ProviderAttemptPermit | None,
    *,
    disposition: ProviderAttemptDisposition,
    outcome: ProviderAttemptOutcome,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    if controller is not None:
        assert permit is not None
        await controller.finish(
            permit,
            disposition=disposition,
            outcome=outcome,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _consume_task_result(task: asyncio.Task[None]) -> None:
    with suppress(BaseException):
        task.result()


async def _finish_provider_attempt_after_cancellation(
    controller: ProviderAttemptController | None,
    permit: ProviderAttemptPermit | None,
    *,
    disposition: ProviderAttemptDisposition,
    outcome: ProviderAttemptOutcome,
) -> bool:
    """Try one bounded, cancellation-shielded finish before reconciliation."""

    if controller is None:
        return True
    task = asyncio.create_task(
        _finish_provider_attempt(
            controller,
            permit,
            disposition=disposition,
            outcome=outcome,
        )
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_ATTEMPT_FINISH_SHIELD_TIMEOUT_SECONDS,
        )
    except BaseException:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        return False
    return True


@dataclass(frozen=True)
class ModelGenerationResult:
    """A normalized response returned by every model adapter."""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float = 0.0
    provider_request_id: str | None = None
    raw_usage: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class AdapterError(RuntimeError):
    """A safe, classified adapter failure suitable for persistence.

    ``error_message`` must already be sanitized by the adapter.  The class never
    stores a request, response, or headers, which keeps credentials out of its
    representation and traceback locals exposed to callers.
    """

    def __init__(
        self,
        error_type: str,
        error_message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(error_message)
        self.error_type = error_type
        self.error_message = error_message
        self.retryable = retryable
        self.status_code = status_code
        self.attempts = attempts

    @property
    def message(self) -> str:
        """Compatibility alias for consumers that expect ``message``."""

        return self.error_message


class ModelAdapter(ABC):
    """Abstract interface for asynchronous text generation."""

    @abstractmethod
    async def generate(
        self,
        messages: Sequence[Message],
        generation_config: GenerationConfig,
        *,
        attempt_context: ProviderAttemptContext | None = None,
    ) -> ModelGenerationResult:
        """Generate one response for an already-rendered message sequence."""

    async def aclose(self) -> None:
        """Release adapter-owned resources; stateless adapters need no action."""

        return None
