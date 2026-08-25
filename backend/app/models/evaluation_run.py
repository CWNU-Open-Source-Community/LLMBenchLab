"""Evaluation run state and reproducibility snapshots."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import PROTOCOL_VERSION
from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UTCDateTime
from app.models.enums import RunStatus

if TYPE_CHECKING:
    from app.models.benchmark import Benchmark
    from app.models.evaluation_response import EvaluationResponse
    from app.models.model import Model


def _uuid() -> str:
    return str(uuid4())


class EvaluationRun(Base):
    """A persisted evaluation execution with immutable configuration snapshots."""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint("total_questions >= 0", name="total_questions_nonnegative"),
        CheckConstraint("completed_questions >= 0", name="completed_questions_nonnegative"),
        CheckConstraint("correct_questions >= 0", name="correct_questions_nonnegative"),
        CheckConstraint("error_questions >= 0", name="error_questions_nonnegative"),
        CheckConstraint("completed_questions <= total_questions", name="completed_not_above_total"),
        CheckConstraint(
            "correct_questions <= completed_questions", name="correct_not_above_completed"
        ),
        CheckConstraint(
            "error_questions <= completed_questions", name="errors_not_above_completed"
        ),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="score_range"),
        CheckConstraint(
            "completion_rate IS NULL OR (completion_rate >= 0 AND completion_rate <= 100)",
            name="completion_rate_range",
        ),
        CheckConstraint(
            "answered_accuracy IS NULL OR (answered_accuracy >= 0 AND answered_accuracy <= 100)",
            name="answered_accuracy_range",
        ),
        CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        CheckConstraint("estimated_cost >= 0", name="estimated_cost_nonnegative"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("attempt_count <= max_attempts", name="attempt_within_limit"),
        CheckConstraint("lease_token >= 0", name="lease_token_nonnegative"),
        CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL) OR "
            "(status <> 'running' AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)",
            name="lease_matches_running_status",
        ),
        CheckConstraint(
            "next_attempt_at IS NULL OR status = 'pending'",
            name="next_attempt_only_pending",
        ),
        CheckConstraint(
            "dead_lettered_at IS NULL OR status = 'failed'",
            name="dead_letter_only_failed",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="status_values",
        ),
        Index("ix_evaluation_runs_status_created", "status", "created_at"),
        Index(
            "ix_evaluation_runs_comparison_partition",
            "benchmark_id",
            "protocol_version",
            "benchmark_hash_snapshot",
        ),
        Index("ix_evaluation_runs_model_created", "model_id", "created_at"),
        Index(
            "ix_evaluation_runs_dispatch_due",
            "status",
            "cancellation_requested",
            "next_attempt_at",
            "created_at",
        ),
        Index(
            "ix_evaluation_runs_lease_expiry",
            "status",
            "cancellation_requested",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    model_id: Mapped[str] = mapped_column(
        ForeignKey("models.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    benchmark_id: Mapped[str] = mapped_column(
        ForeignKey("benchmarks.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[RunStatus] = mapped_column(
        Enum(
            RunStatus,
            name="run_status",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=RunStatus.PENDING,
        nullable=False,
        index=True,
    )
    protocol_version: Mapped[str] = mapped_column(
        String(64), default=PROTOCOL_VERSION, nullable=False
    )
    model_parameters_snapshot: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    benchmark_hash_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_template_snapshot: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    code_commit_sha: Mapped[str | None] = mapped_column(String(64))
    total_questions: Mapped[int] = mapped_column(default=0, nullable=False)
    completed_questions: Mapped[int] = mapped_column(default=0, nullable=False)
    correct_questions: Mapped[int] = mapped_column(default=0, nullable=False)
    error_questions: Mapped[int] = mapped_column(default=0, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    completion_rate: Mapped[float | None] = mapped_column(Float)
    answered_accuracy: Mapped[float | None] = mapped_column(Float)
    average_latency_ms: Mapped[float | None] = mapped_column(Float)
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_enqueued_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(Text)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)

    model: Mapped[Model] = relationship(back_populates="runs")
    benchmark: Mapped[Benchmark] = relationship(back_populates="runs")
    responses: Mapped[list[EvaluationResponse]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="EvaluationResponse.created_at",
    )
