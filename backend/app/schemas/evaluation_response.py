"""Public evidence schema for per-question responses."""

from datetime import datetime
from typing import Any

from pydantic import field_validator

from app.schemas.base import ORMModel
from app.security import normalize_http_attempt_count, normalize_provider_metadata


class EvaluationResponseRead(ORMModel):
    id: str
    run_id: str
    question_id: str
    raw_response: str | None
    parsed_answer: Any | None
    reference_answer_snapshot: Any
    score: float
    evaluator_name: str
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: float | None
    provider_request_id: str | None
    returned_model: str | None
    system_fingerprint: str | None
    finish_reason: str | None
    http_attempt_count: int | None
    error_type: str | None
    error_message: str | None
    created_at: datetime

    @field_validator(
        "provider_request_id",
        "returned_model",
        "system_fingerprint",
        mode="before",
    )
    @classmethod
    def provider_metadata_must_be_safe_256(cls, value: object) -> str | None:
        return normalize_provider_metadata(value, max_length=256)

    @field_validator("finish_reason", mode="before")
    @classmethod
    def finish_reason_must_be_safe(cls, value: object) -> str | None:
        return normalize_provider_metadata(value, max_length=128)

    @field_validator("http_attempt_count", mode="before")
    @classmethod
    def attempts_must_be_positive(cls, value: object) -> int | None:
        return normalize_http_attempt_count(value)


class EvaluationResponseDetail(EvaluationResponseRead):
    """Per-question evidence enriched with immutable question display fields."""

    question_external_id: str
    question_type: str
    prompt: str
    choices: dict[str, str] | None


class EvaluationResponseList(ORMModel):
    items: list[EvaluationResponseDetail]
    total: int
    offset: int
    limit: int
