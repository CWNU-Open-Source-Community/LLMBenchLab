"""Request and response schemas for evaluation runs."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator

from app.core.constants import (
    DEFAULT_READ_TIMEOUT_SECONDS,
    MAX_GENERATION_TOKENS,
    MAX_GOVERNANCE_COST_USD,
    MAX_READ_TIMEOUT_SECONDS,
    MIN_READ_TIMEOUT_SECONDS,
)
from app.models.enums import RunStatus
from app.schemas.base import APIModel, ORMModel


class EvaluationRunCreate(APIModel):
    model_id: str = Field(min_length=1, max_length=36)
    benchmark_id: str = Field(min_length=1, max_length=36)
    temperature: float = Field(default=0.0, ge=0, le=2)
    top_p: float = Field(default=1.0, gt=0, le=1)
    max_tokens: int | None = Field(default=256, ge=1, le=MAX_GENERATION_TOKENS)
    seed: int | None = Field(default=42, ge=-(2**31), le=2**31 - 1)
    system_prompt: str | None = Field(default=None, max_length=4000)
    concurrency: int = Field(default=1, ge=1, le=4)
    input_token_reservation: int | None = Field(default=None, ge=1, le=10_000_000)
    lifetime_request_budget: int | None = Field(default=None, ge=0, le=1_000_000_000)
    lifetime_token_budget: int | None = Field(default=None, ge=0, le=10_000_000_000_000)
    lifetime_cost_budget_usd: Decimal | None = Field(
        default=None,
        ge=Decimal(0),
        le=MAX_GOVERNANCE_COST_USD,
        decimal_places=8,
    )
    read_timeout_seconds: float = Field(
        default=DEFAULT_READ_TIMEOUT_SECONDS,
        ge=MIN_READ_TIMEOUT_SECONDS,
        le=MAX_READ_TIMEOUT_SECONDS,
    )

    @field_validator("max_tokens", mode="before")
    @classmethod
    def validate_max_tokens_json_type(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("max_tokens must be null or an integer")
        return value

    @field_validator("read_timeout_seconds", mode="before")
    @classmethod
    def validate_read_timeout_json_type(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("read_timeout_seconds must be a number")
        return value

    @field_validator(
        "input_token_reservation",
        "lifetime_request_budget",
        "lifetime_token_budget",
        mode="before",
    )
    @classmethod
    def validate_governance_integer_json_type(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("governance limits must be null or an integer")
        return value


class EvaluationRunRead(ORMModel):
    id: str
    model_id: str
    benchmark_id: str
    status: RunStatus
    protocol_version: str
    model_parameters_snapshot: dict[str, Any]
    benchmark_hash_snapshot: str
    prompt_template_snapshot: dict[str, Any]
    code_commit_sha: str | None
    total_questions: int
    completed_questions: int
    correct_questions: int
    error_questions: int
    score: float | None
    completion_rate: float | None
    answered_accuracy: float | None
    average_latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: float | None
    cancellation_requested: bool
    attempt_count: int
    max_attempts: int
    failed_attempt_count: int
    dispatch_count: int
    last_scheduled_at: datetime | None
    governance_policy_id: str | None
    governance_status: str
    governance_reason: str | None
    governance_not_before: datetime | None
    input_token_reservation: int | None
    lifetime_request_budget: int | None
    lifetime_token_budget: int | None
    lifetime_cost_budget_usd: Decimal | None
    lease_owner: str | None
    lease_token: int
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    next_attempt_at: datetime | None
    last_enqueued_at: datetime | None
    last_error: str | None
    dead_lettered_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    error_message: str | None


class EvaluationRunList(ORMModel):
    items: list[EvaluationRunRead]
    total: int
    offset: int
    limit: int
