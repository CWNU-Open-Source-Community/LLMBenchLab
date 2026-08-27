"""Explicit full-document governance policy API schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import Field

from app.core.constants import MAX_GOVERNANCE_COST_USD
from app.schemas.base import APIModel, ORMModel

_INT32_MAX = 2**31 - 1
_INT64_MAX = 2**63 - 1
_NONNEGATIVE_INT32 = Annotated[int, Field(strict=True, ge=0, le=_INT32_MAX)]
_NONNEGATIVE_INT64 = Annotated[int, Field(strict=True, ge=0, le=_INT64_MAX)]
_POSITIVE_INT32 = Annotated[int, Field(strict=True, ge=1, le=_INT32_MAX)]


class GovernancePolicyApply(APIModel):
    """A complete replacement policy; null disables a limit and zero denies."""

    global_concurrency_limit: _NONNEGATIVE_INT32 | None = Field(...)
    provider_concurrency_limit: _NONNEGATIVE_INT32 | None = Field(...)
    model_concurrency_limit: _NONNEGATIVE_INT32 | None = Field(...)
    run_concurrency_limit: _NONNEGATIVE_INT32 | None = Field(...)
    global_requests_per_minute: _NONNEGATIVE_INT64 | None = Field(...)
    provider_requests_per_minute: _NONNEGATIVE_INT64 | None = Field(...)
    model_requests_per_minute: _NONNEGATIVE_INT64 | None = Field(...)
    run_requests_per_minute: _NONNEGATIVE_INT64 | None = Field(...)
    global_tokens_per_minute: _NONNEGATIVE_INT64 | None = Field(...)
    provider_tokens_per_minute: _NONNEGATIVE_INT64 | None = Field(...)
    model_tokens_per_minute: _NONNEGATIVE_INT64 | None = Field(...)
    run_tokens_per_minute: _NONNEGATIVE_INT64 | None = Field(...)
    global_lifetime_request_budget: _NONNEGATIVE_INT64 | None = Field(...)
    global_lifetime_token_budget: _NONNEGATIVE_INT64 | None = Field(...)
    global_lifetime_cost_budget_usd: Decimal | None = Field(
        ...,
        ge=Decimal(0),
        le=MAX_GOVERNANCE_COST_USD,
        max_digits=20,
        decimal_places=8,
    )
    run_lifetime_request_budget: _NONNEGATIVE_INT64 | None = Field(...)
    run_lifetime_token_budget: _NONNEGATIVE_INT64 | None = Field(...)
    run_lifetime_cost_budget_usd: Decimal | None = Field(
        ...,
        ge=Decimal(0),
        le=MAX_GOVERNANCE_COST_USD,
        max_digits=20,
        decimal_places=8,
    )
    backlog_limit: _NONNEGATIVE_INT32
    question_quantum: _POSITIVE_INT32


class GovernancePolicyRead(ORMModel):
    id: str
    version: int
    policy_hash: str
    is_active: bool
    global_concurrency_limit: int | None
    provider_concurrency_limit: int | None
    model_concurrency_limit: int | None
    run_concurrency_limit: int | None
    global_requests_per_minute: int | None
    provider_requests_per_minute: int | None
    model_requests_per_minute: int | None
    run_requests_per_minute: int | None
    global_tokens_per_minute: int | None
    provider_tokens_per_minute: int | None
    model_tokens_per_minute: int | None
    run_tokens_per_minute: int | None
    global_lifetime_request_budget: int | None
    global_lifetime_token_budget: int | None
    global_lifetime_cost_budget_usd: Decimal | None
    run_lifetime_request_budget: int | None
    run_lifetime_token_budget: int | None
    run_lifetime_cost_budget_usd: Decimal | None
    backlog_limit: int
    question_quantum: int
    activated_at: datetime | None
    created_at: datetime
