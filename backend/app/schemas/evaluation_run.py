"""Request and response schemas for evaluation runs."""

from datetime import datetime
from typing import Any

from pydantic import Field

from app.models.enums import RunStatus
from app.schemas.base import APIModel, ORMModel


class EvaluationRunCreate(APIModel):
    model_id: str = Field(min_length=1, max_length=36)
    benchmark_id: str = Field(min_length=1, max_length=36)
    temperature: float = Field(default=0.0, ge=0, le=2)
    top_p: float = Field(default=1.0, gt=0, le=1)
    max_tokens: int = Field(default=256, ge=1, le=32_768)
    seed: int | None = Field(default=42, ge=-(2**31), le=2**31 - 1)
    system_prompt: str | None = Field(default=None, max_length=4000)
    concurrency: int = Field(default=1, ge=1, le=4)


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
