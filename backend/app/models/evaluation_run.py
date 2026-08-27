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
    func,
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import PROTOCOL_VERSION
from app.db.base import Base
from app.db.types import UTCDateTime
from app.models.enums import GovernanceRunStatus, RunStatus

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
        CheckConstraint("failed_attempt_count >= 0", name="failed_attempt_count_nonnegative"),
        CheckConstraint(
            "failed_attempt_count <= max_attempts", name="failed_attempt_count_within_limit"
        ),
        CheckConstraint("dispatch_count >= 0", name="dispatch_count_nonnegative"),
        CheckConstraint(
            "input_token_reservation IS NULL OR input_token_reservation >= 0",
            name="input_token_reservation_nonnegative",
        ),
        CheckConstraint(
            "lifetime_request_budget IS NULL OR lifetime_request_budget >= 0",
            name="lifetime_request_budget_nonnegative",
        ),
        CheckConstraint(
            "lifetime_token_budget IS NULL OR lifetime_token_budget >= 0",
            name="lifetime_token_budget_nonnegative",
        ),
        CheckConstraint(
            "lifetime_cost_budget_usd IS NULL OR lifetime_cost_budget_usd >= 0",
            name="lifetime_cost_budget_nonnegative",
        ),
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
            "governance_status IN ('legacy_unmanaged', 'managed', 'delayed', 'exhausted')",
            name="governance_status_values",
        ),
        CheckConstraint(
            "(governance_status = 'legacy_unmanaged' AND governance_policy_id IS NULL) OR "
            "(governance_status <> 'legacy_unmanaged' AND governance_policy_id IS NOT NULL)",
            name="governance_policy_matches_status",
        ),
        CheckConstraint(
            "governance_not_before IS NULL OR "
            "(status = 'pending' AND governance_status = 'delayed')",
            name="governance_delay_matches_pending",
        ),
        CheckConstraint(
            "governance_status <> 'exhausted' OR status = 'failed'",
            name="governance_exhausted_is_failed",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="status_values",
        ),
        Index("ix_evaluation_runs_started_at_id", "started_at", "id"),
        Index("ix_evaluation_runs_finished_at_id", "finished_at", "id"),
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
        Index(
            "ix_evaluation_runs_governance_dispatch",
            "status",
            "governance_status",
            "governance_not_before",
            "last_scheduled_at",
            "created_at",
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
    failed_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dispatch_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    governance_policy_id: Mapped[str | None] = mapped_column(
        ForeignKey("governance_policies.id", ondelete="RESTRICT"), index=True
    )
    governance_status: Mapped[GovernanceRunStatus] = mapped_column(
        Enum(
            GovernanceRunStatus,
            name="governance_run_status",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=GovernanceRunStatus.LEGACY_UNMANAGED,
        nullable=False,
    )
    governance_reason: Mapped[str | None] = mapped_column(String(128))
    governance_not_before: Mapped[datetime | None] = mapped_column(UTCDateTime())
    input_token_reservation: Mapped[int | None] = mapped_column(BigInteger)
    lifetime_request_budget: Mapped[int | None] = mapped_column(BigInteger)
    lifetime_token_budget: Mapped[int | None] = mapped_column(BigInteger)
    lifetime_cost_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
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
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=func.current_timestamp(), nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    model: Mapped[Model] = relationship(back_populates="runs")
    benchmark: Mapped[Benchmark] = relationship(back_populates="runs")
    responses: Mapped[list[EvaluationResponse]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="EvaluationResponse.created_at",
    )
