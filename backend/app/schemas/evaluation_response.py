"""Public evidence schema for per-question responses."""

from datetime import datetime
from typing import Any

from app.schemas.base import ORMModel


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
    error_type: str | None
    error_message: str | None
    created_at: datetime


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
