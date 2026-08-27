"""Provider-independent model generation contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

Message = Mapping[str, Any]
GenerationConfig = Mapping[str, Any]


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
    ) -> ModelGenerationResult:
        """Generate one response for an already-rendered message sequence."""

    async def aclose(self) -> None:
        """Release adapter-owned resources; stateless adapters need no action."""

        return None
