"""Secret-free contracts shared by Provider adapters and governance.

This module deliberately has no adapter imports so the API process can perform
Run admission without loading HTTP clients or execution-only adapter modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProviderAttemptContext:
    """Request-local governance input containing no credential or payload data."""

    run_id: str
    question_id: str
    model_id: str
    provider_scope: str
    lease_token: int
    execution_generation: int
    next_provider_attempt: int
    reserved_input_tokens: int | None = None
    reserved_output_tokens: int | None = None
    reserved_cost_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ProviderAttemptPermit:
    """Opaque, secret-free handle returned by a governance reservation."""

    reservation_id: str
    provider_attempt: int


class ProviderAttemptDisposition(StrEnum):
    """Terminal accounting disposition for one Provider HTTP attempt."""

    RELEASED_PRE_SEND = "released_pre_send"
    SETTLED_ACTUAL = "settled_actual"
    SETTLED_CONSERVATIVE = "settled_conservative"


class ProviderAttemptOutcome(StrEnum):
    """Allowlisted, non-secret reason associated with attempt accounting."""

    SUCCEEDED = "succeeded"
    USAGE_INCOMPLETE = "usage_incomplete"
    TRANSPORT_ERROR = "transport_error"
    HTTP_ERROR = "http_error"
    PROVIDER_RESPONSE_ERROR = "provider_response_error"
    CANCELLED = "cancelled"
    MARK_SEND_FAILED = "mark_send_failed"
    UNEXPECTED_ERROR = "unexpected_error"


class ProviderAttemptStateUnknown(RuntimeError):
    """A reservation state transition may have committed without acknowledgement.

    Adapters must stop and leave reconciliation to the durable controller. In
    particular, they must not guess that a failed ``mark_send_started`` remained
    pre-send and release the reservation.
    """


class ProviderAttemptController(Protocol):
    """Durable three-stage admission and accounting hook used by adapters."""

    async def reserve(
        self,
        context: ProviderAttemptContext,
        *,
        provider_attempt: int,
    ) -> ProviderAttemptPermit:
        """Reserve capacity for one logical Provider attempt."""

        ...

    async def mark_send_started(self, permit: ProviderAttemptPermit) -> None:
        """Durably mark that the Provider may process the request."""

        ...

    async def finish(
        self,
        permit: ProviderAttemptPermit,
        *,
        disposition: ProviderAttemptDisposition,
        outcome: ProviderAttemptOutcome,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """Release or settle a reserved Provider attempt exactly once."""

        ...
