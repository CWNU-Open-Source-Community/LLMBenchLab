"""Persisted evidence for one evaluated question."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UTCDateTime

if TYPE_CHECKING:
    from app.models.evaluation_run import EvaluationRun
    from app.models.question import Question


def _uuid() -> str:
    return str(uuid4())


class EvaluationResponse(Base):
    """Raw generation, parsed answer, objective score, usage, and any error."""

    __tablename__ = "evaluation_responses"
    __table_args__ = (
        UniqueConstraint("run_id", "question_id", name="uq_responses_run_question"),
        CheckConstraint("score >= 0 AND score <= 1", name="score_range"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_nonnegative"),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0", name="input_tokens_nonnegative"
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0", name="output_tokens_nonnegative"
        ),
        CheckConstraint(
            "estimated_cost IS NULL OR estimated_cost >= 0", name="estimated_cost_nonnegative"
        ),
        CheckConstraint(
            "http_attempt_count IS NULL OR http_attempt_count >= 1",
            name="http_attempt_count_positive",
        ),
        Index("ix_evaluation_responses_run_created", "run_id", "created_at"),
        Index("ix_evaluation_responses_question", "question_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_id: Mapped[str] = mapped_column(
        ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    raw_response: Mapped[str | None] = mapped_column(Text)
    parsed_answer: Mapped[Any | None] = mapped_column(JSON)
    reference_answer_snapshot: Mapped[Any] = mapped_column(JSON, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    evaluator_name: Mapped[str] = mapped_column(String(128), nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    provider_request_id: Mapped[str | None] = mapped_column(String(256))
    returned_model: Mapped[str | None] = mapped_column(String(256))
    system_fingerprint: Mapped[str | None] = mapped_column(String(256))
    finish_reason: Mapped[str | None] = mapped_column(String(128))
    http_attempt_count: Mapped[int | None] = mapped_column()
    error_type: Mapped[str | None] = mapped_column(String(128), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    run: Mapped[EvaluationRun] = relationship(back_populates="responses")
    question: Mapped[Question] = relationship(back_populates="responses")
