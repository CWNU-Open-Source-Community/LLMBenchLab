"""Database-authoritative provider governance, attempt ledger, and audit entities."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
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
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UTCDateTime
from app.models.enums import (
    AuditRetentionClass,
    GovernanceScopeType,
    ProviderCallReservationState,
)
from app.models.mixins import TimestampMixin


def _uuid() -> str:
    return str(uuid4())


_SCOPE_VALUES = "'global', 'provider', 'model', 'run'"
_RESERVATION_STATE_VALUES = (
    "'reserved', 'send_started', 'settled_actual', 'settled_conservative', 'released_pre_send'"
)


class GovernancePolicy(Base):
    """An immutable, versioned set of database admission limits."""

    __tablename__ = "governance_policies"
    __table_args__ = (
        UniqueConstraint("version", name="uq_governance_policies_version"),
        UniqueConstraint("policy_hash", name="uq_governance_policies_policy_hash"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("length(policy_hash) = 64", name="policy_hash_length"),
        CheckConstraint("backlog_limit >= 0", name="backlog_limit_nonnegative"),
        CheckConstraint("question_quantum >= 1", name="question_quantum_positive"),
        CheckConstraint(
            "is_active = false OR activated_at IS NOT NULL",
            name="active_requires_activation_time",
        ),
        *(
            CheckConstraint(
                f"{column} IS NULL OR {column} >= 0",
                name={
                    "global_lifetime_request_budget": "global_lifetime_request_nonnegative",
                    "global_lifetime_cost_budget_usd": "global_lifetime_cost_nonnegative",
                }.get(column, f"{column}_nonnegative"),
            )
            for column in (
                "global_concurrency_limit",
                "provider_concurrency_limit",
                "model_concurrency_limit",
                "run_concurrency_limit",
                "global_requests_per_minute",
                "provider_requests_per_minute",
                "model_requests_per_minute",
                "run_requests_per_minute",
                "global_tokens_per_minute",
                "provider_tokens_per_minute",
                "model_tokens_per_minute",
                "run_tokens_per_minute",
                "global_lifetime_request_budget",
                "global_lifetime_token_budget",
                "global_lifetime_cost_budget_usd",
                "run_lifetime_request_budget",
                "run_lifetime_token_budget",
                "run_lifetime_cost_budget_usd",
            )
        ),
        Index("ix_governance_policies_created", "created_at"),
        Index(
            "uq_governance_policies_single_active",
            "is_active",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active IS TRUE"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    global_concurrency_limit: Mapped[int | None] = mapped_column(Integer)
    provider_concurrency_limit: Mapped[int | None] = mapped_column(Integer)
    model_concurrency_limit: Mapped[int | None] = mapped_column(Integer)
    run_concurrency_limit: Mapped[int | None] = mapped_column(Integer)

    global_requests_per_minute: Mapped[int | None] = mapped_column(BigInteger)
    provider_requests_per_minute: Mapped[int | None] = mapped_column(BigInteger)
    model_requests_per_minute: Mapped[int | None] = mapped_column(BigInteger)
    run_requests_per_minute: Mapped[int | None] = mapped_column(BigInteger)
    global_tokens_per_minute: Mapped[int | None] = mapped_column(BigInteger)
    provider_tokens_per_minute: Mapped[int | None] = mapped_column(BigInteger)
    model_tokens_per_minute: Mapped[int | None] = mapped_column(BigInteger)
    run_tokens_per_minute: Mapped[int | None] = mapped_column(BigInteger)

    global_lifetime_request_budget: Mapped[int | None] = mapped_column(BigInteger)
    global_lifetime_token_budget: Mapped[int | None] = mapped_column(BigInteger)
    global_lifetime_cost_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    run_lifetime_request_budget: Mapped[int | None] = mapped_column(BigInteger)
    run_lifetime_token_budget: Mapped[int | None] = mapped_column(BigInteger)
    run_lifetime_cost_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))

    backlog_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    question_quantum: Mapped[int] = mapped_column(Integer, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class GovernanceScope(TimestampMixin, Base):
    """A stable four-level lock anchor with ledger-recomputable materialized totals."""

    __tablename__ = "governance_scopes"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_key", name="uq_governance_scopes_type_key"),
        CheckConstraint(f"scope_type IN ({_SCOPE_VALUES})", name="scope_type_values"),
        CheckConstraint("length(scope_key) >= 1", name="scope_key_nonempty"),
        *(
            CheckConstraint(f"{column} >= 0", name=f"{column}_nonnegative")
            for column in (
                "active_reservations",
                "reserved_requests",
                "reserved_input_tokens",
                "reserved_output_tokens",
                "consumed_requests",
                "consumed_input_tokens",
                "consumed_output_tokens",
                "reserved_cost_usd",
                "consumed_cost_usd",
            )
        ),
        Index("ix_governance_scopes_type_key", "scope_type", "scope_key"),
        Index("ix_governance_scopes_overdrawn", "overdrawn", "scope_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope_type: Mapped[GovernanceScopeType] = mapped_column(
        Enum(
            GovernanceScopeType,
            name="governance_scope_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    active_reservations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_requests: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal(0), nullable=False
    )
    consumed_requests: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    consumed_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    consumed_output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    consumed_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal(0), nullable=False
    )
    overdrawn: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class GovernanceMinuteBucket(TimestampMixin, Base):
    """Per-policy fixed UTC-minute materialization for one governance scope."""

    __tablename__ = "governance_minute_buckets"
    __table_args__ = (
        UniqueConstraint(
            "scope_id",
            "policy_id",
            "window_start",
            name="uq_governance_minute_buckets_scope_policy_window",
        ),
        *(
            CheckConstraint(f"{column} >= 0", name=f"{column}_nonnegative")
            for column in (
                "reserved_requests",
                "reserved_input_tokens",
                "reserved_output_tokens",
                "consumed_requests",
                "consumed_input_tokens",
                "consumed_output_tokens",
            )
        ),
        Index("ix_governance_minute_buckets_window", "window_start", "scope_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope_id: Mapped[str] = mapped_column(
        ForeignKey("governance_scopes.id", ondelete="RESTRICT"), nullable=False
    )
    policy_id: Mapped[str] = mapped_column(
        ForeignKey("governance_policies.id", ondelete="RESTRICT"), nullable=False
    )
    window_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reserved_requests: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    consumed_requests: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    consumed_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    consumed_output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class QuestionExecution(TimestampMixin, Base):
    """Persistent retry generation and ordinal across cooperative Run slices."""

    __tablename__ = "question_executions"
    __table_args__ = (
        UniqueConstraint("run_id", "question_id", name="uq_question_executions_run_question"),
        CheckConstraint("execution_generation >= 0", name="generation_nonnegative"),
        CheckConstraint("next_provider_attempt >= 1", name="next_attempt_positive"),
        Index("ix_question_executions_retry_due", "retry_not_before", "run_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    question_id: Mapped[str] = mapped_column(
        ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    execution_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_provider_attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    retry_not_before: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ProviderCallReservation(TimestampMixin, Base):
    """Never-delete, one-way state ledger for one logical Provider attempt."""

    __tablename__ = "provider_call_reservations"
    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_provider_call_reservations_operation_key"),
        UniqueConstraint(
            "run_id",
            "question_id",
            "execution_generation",
            "provider_attempt",
            name="uq_provider_call_reservations_logical_attempt",
        ),
        CheckConstraint(
            f"state IN ({_RESERVATION_STATE_VALUES})",
            name="state_values",
        ),
        CheckConstraint("execution_generation >= 0", name="generation_nonnegative"),
        CheckConstraint("provider_attempt >= 1", name="provider_attempt_positive"),
        CheckConstraint("lease_token IS NULL OR lease_token >= 0", name="lease_token_nonnegative"),
        CheckConstraint(
            "reserved_input_tokens IS NULL OR reserved_input_tokens >= 0",
            name="reserved_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "reserved_output_tokens IS NULL OR reserved_output_tokens >= 0",
            name="reserved_output_nonnegative",
        ),
        CheckConstraint(
            "reserved_cost_usd IS NULL OR reserved_cost_usd >= 0",
            name="reserved_cost_nonnegative",
        ),
        CheckConstraint(
            "actual_input_tokens IS NULL OR actual_input_tokens >= 0",
            name="actual_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "actual_output_tokens IS NULL OR actual_output_tokens >= 0",
            name="actual_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="actual_cost_nonnegative",
        ),
        CheckConstraint(
            "(state = 'reserved' AND send_started_at IS NULL AND settled_at IS NULL) OR "
            "(state = 'send_started' AND send_started_at IS NOT NULL AND settled_at IS NULL) OR "
            "(state = 'released_pre_send' AND send_started_at IS NULL "
            "AND settled_at IS NOT NULL) OR "
            "(state IN ('settled_actual', 'settled_conservative') "
            "AND send_started_at IS NOT NULL AND settled_at IS NOT NULL)",
            name="state_timestamps_coherent",
        ),
        CheckConstraint(
            "(question_execution_id IS NULL AND run_id IS NULL AND question_id IS NULL "
            "AND run_scope_id IS NULL) OR "
            "(question_execution_id IS NOT NULL AND run_id IS NOT NULL "
            "AND question_id IS NOT NULL AND run_scope_id IS NOT NULL)",
            name="question_execution_coherent",
        ),
        Index("ix_provider_call_reservations_state_lease", "state", "lease_expires_at"),
        Index("ix_provider_call_reservations_run_created", "run_id", "created_at"),
        Index("ix_provider_call_reservations_provider_scope", "provider_scope_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    operation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    policy_id: Mapped[str] = mapped_column(
        ForeignKey("governance_policies.id", ondelete="RESTRICT"), nullable=False
    )
    question_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("question_executions.id", ondelete="RESTRICT")
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), index=True
    )
    question_id: Mapped[str | None] = mapped_column(
        ForeignKey("questions.id", ondelete="RESTRICT"), index=True
    )
    model_id: Mapped[str] = mapped_column(
        ForeignKey("models.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    global_scope_id: Mapped[str] = mapped_column(
        ForeignKey("governance_scopes.id", ondelete="RESTRICT"), nullable=False
    )
    provider_scope_id: Mapped[str] = mapped_column(
        ForeignKey("governance_scopes.id", ondelete="RESTRICT"), nullable=False
    )
    model_scope_id: Mapped[str] = mapped_column(
        ForeignKey("governance_scopes.id", ondelete="RESTRICT"), nullable=False
    )
    run_scope_id: Mapped[str | None] = mapped_column(
        ForeignKey("governance_scopes.id", ondelete="RESTRICT")
    )
    execution_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[ProviderCallReservationState] = mapped_column(
        Enum(
            ProviderCallReservationState,
            name="provider_call_reservation_state",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=ProviderCallReservationState.RESERVED,
        nullable=False,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    window_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reserved_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    reserved_output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    reserved_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    actual_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    actual_output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    outcome_code: Mapped[str | None] = mapped_column(String(128))
    send_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditEvent(Base):
    """Application append-only, typed operational or security event."""

    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_audit_events_event_key"),
        CheckConstraint(
            "retention_class IN ('operational', 'security')",
            name="retention_class_values",
        ),
        CheckConstraint("length(payload_hash) = 64", name="payload_hash_length"),
        CheckConstraint("attempt IS NULL OR attempt >= 0", name="attempt_nonnegative"),
        CheckConstraint(
            "provider_attempt IS NULL OR provider_attempt >= 1",
            name="provider_attempt_positive",
        ),
        CheckConstraint("lease_token IS NULL OR lease_token >= 0", name="lease_token_nonnegative"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_nonnegative"),
        CheckConstraint("expires_at > occurred_at", name="expiry_after_occurrence"),
        Index("ix_audit_events_run_occurred", "run_id", "occurred_at", "id"),
        Index("ix_audit_events_type_occurred", "event_type", "occurred_at"),
        Index("ix_audit_events_expiry", "expires_at", "retention_class"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    retention_class: Mapped[AuditRetentionClass] = mapped_column(
        Enum(
            AuditRetentionClass,
            name="audit_retention_class",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    run_id: Mapped[str | None] = mapped_column(String(36))
    model_id: Mapped[str | None] = mapped_column(String(36))
    question_id: Mapped[str | None] = mapped_column(String(36))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    reservation_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_call_reservations.id", ondelete="RESTRICT")
    )
    attempt: Mapped[int | None] = mapped_column(Integer)
    provider_attempt: Mapped[int | None] = mapped_column(Integer)
    lease_token: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[float | None] = mapped_column(Float)
