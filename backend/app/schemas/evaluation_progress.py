"""Compact, body-free progress projections for one Evaluation Run."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from app.schemas.base import ORMModel

PROGRESS_BLOCK_SIZE = 512


class EvaluationProgressOutcome(StrEnum):
    """Disjoint terminal outcome for one persisted Response."""

    PASSED = "passed"
    WRONG = "wrong"
    ERROR = "error"


class EvaluationProgressBlockSummary(ORMModel):
    """Number of persisted Responses currently present in one planned block."""

    block_index: int = Field(ge=0)
    response_count: int = Field(ge=0, le=PROGRESS_BLOCK_SIZE)


class EvaluationProgressIndex(ORMModel):
    """Canonical live metrics plus a compact change index for fixed-size blocks."""

    block_size: Literal[PROGRESS_BLOCK_SIZE]
    total_questions: int = Field(ge=0)
    completed_questions: int = Field(ge=0)
    correct_questions: int = Field(ge=0)
    error_questions: int = Field(ge=0)
    score: float = Field(ge=0, le=100)
    completion_rate: float = Field(ge=0, le=100)
    answered_accuracy: float | None = Field(ge=0, le=100)
    average_latency_ms: float | None = Field(ge=0)
    known_input_tokens: int = Field(ge=0)
    known_output_tokens: int = Field(ge=0)
    input_token_reported_responses: int = Field(ge=0)
    output_token_reported_responses: int = Field(ge=0)
    known_estimated_cost: float = Field(ge=0)
    estimated_cost_reported_responses: int = Field(ge=0)
    blocks: list[EvaluationProgressBlockSummary]


class EvaluationProgressCell(ORMModel):
    """Allowlisted tooltip facts for one completed absolute question position."""

    position: int = Field(ge=0)
    outcome: EvaluationProgressOutcome
    score: float = Field(ge=0, le=1)
    latency_ms: float | None = Field(ge=0)
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    estimated_cost: float | None = Field(ge=0)
    error_type: str | None = Field(max_length=128)


class EvaluationProgressBlock(ORMModel):
    """All persisted compact cells for one fixed absolute-position block."""

    block_index: int = Field(ge=0)
    items: list[EvaluationProgressCell]
