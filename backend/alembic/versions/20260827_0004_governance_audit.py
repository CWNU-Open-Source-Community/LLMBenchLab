"""Add database-authoritative governance, provider attempt ledger, and audit evidence.

Revision ID: 20260827_0004
Revises: 20260827_0003
Create Date: 2026-08-27 18:00:00 UTC
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260827_0004"
down_revision: str | None = "20260827_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _check(table: str, name: str, expression: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _fk(
    table: str,
    column: str,
    target_table: str,
    *,
    target_column: str = "id",
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"{target_table}.{target_column}"],
        name=op.f(f"fk_{table}_{column}_{target_table}"),
        ondelete="RESTRICT",
    )


def _create_governance_policy_table() -> None:
    table = "governance_policies"
    columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("global_concurrency_limit", sa.Integer(), nullable=True),
        sa.Column("provider_concurrency_limit", sa.Integer(), nullable=True),
        sa.Column("model_concurrency_limit", sa.Integer(), nullable=True),
        sa.Column("run_concurrency_limit", sa.Integer(), nullable=True),
        sa.Column("global_requests_per_minute", sa.BigInteger(), nullable=True),
        sa.Column("provider_requests_per_minute", sa.BigInteger(), nullable=True),
        sa.Column("model_requests_per_minute", sa.BigInteger(), nullable=True),
        sa.Column("run_requests_per_minute", sa.BigInteger(), nullable=True),
        sa.Column("global_tokens_per_minute", sa.BigInteger(), nullable=True),
        sa.Column("provider_tokens_per_minute", sa.BigInteger(), nullable=True),
        sa.Column("model_tokens_per_minute", sa.BigInteger(), nullable=True),
        sa.Column("run_tokens_per_minute", sa.BigInteger(), nullable=True),
        sa.Column("global_lifetime_request_budget", sa.BigInteger(), nullable=True),
        sa.Column("global_lifetime_token_budget", sa.BigInteger(), nullable=True),
        sa.Column("global_lifetime_cost_budget_usd", sa.Numeric(20, 8), nullable=True),
        sa.Column("run_lifetime_request_budget", sa.BigInteger(), nullable=True),
        sa.Column("run_lifetime_token_budget", sa.BigInteger(), nullable=True),
        sa.Column("run_lifetime_cost_budget_usd", sa.Numeric(20, 8), nullable=True),
        sa.Column("backlog_limit", sa.Integer(), nullable=False),
        sa.Column("question_quantum", sa.Integer(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _check(table, "version_positive", "version >= 1"),
        _check(table, "policy_hash_length", "length(policy_hash) = 64"),
        _check(table, "backlog_limit_nonnegative", "backlog_limit >= 0"),
        _check(table, "question_quantum_positive", "question_quantum >= 1"),
        _check(
            table,
            "active_requires_activation_time",
            "is_active = false OR activated_at IS NOT NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governance_policies")),
        sa.UniqueConstraint("version", name=op.f("uq_governance_policies_version")),
        sa.UniqueConstraint("policy_hash", name=op.f("uq_governance_policies_policy_hash")),
    ]
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
    ):
        constraint_name = {
            "global_lifetime_request_budget": "global_lifetime_request_nonnegative",
            "global_lifetime_cost_budget_usd": "global_lifetime_cost_nonnegative",
        }.get(column, f"{column}_nonnegative")
        columns.append(_check(table, constraint_name, f"{column} IS NULL OR {column} >= 0"))
    op.create_table(table, *columns)
    op.create_index("ix_governance_policies_created", table, ["created_at"], unique=False)
    op.create_index(
        "uq_governance_policies_single_active",
        table,
        ["is_active"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
        postgresql_where=sa.text("is_active IS TRUE"),
    )


def _create_governance_scope_table() -> None:
    table = "governance_scopes"
    columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=8), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("active_reservations", sa.Integer(), nullable=False),
        sa.Column("reserved_requests", sa.BigInteger(), nullable=False),
        sa.Column("reserved_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_cost_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("consumed_requests", sa.BigInteger(), nullable=False),
        sa.Column("consumed_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("consumed_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("consumed_cost_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("overdrawn", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        _check(
            table,
            "scope_type_values",
            "scope_type IN ('global', 'provider', 'model', 'run')",
        ),
        _check(table, "scope_key_nonempty", "length(scope_key) >= 1"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governance_scopes")),
        sa.UniqueConstraint(
            "scope_type",
            "scope_key",
            name=op.f("uq_governance_scopes_type_key"),
        ),
    ]
    for column in (
        "active_reservations",
        "reserved_requests",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_cost_usd",
        "consumed_requests",
        "consumed_input_tokens",
        "consumed_output_tokens",
        "consumed_cost_usd",
    ):
        columns.append(_check(table, f"{column}_nonnegative", f"{column} >= 0"))
    op.create_table(table, *columns)
    op.create_index(
        "ix_governance_scopes_type_key", table, ["scope_type", "scope_key"], unique=False
    )
    op.create_index(
        "ix_governance_scopes_overdrawn", table, ["overdrawn", "scope_type"], unique=False
    )


def _add_run_governance_columns() -> None:
    for column in (
        sa.Column("failed_attempt_count", sa.Integer(), nullable=True),
        sa.Column("dispatch_count", sa.BigInteger(), nullable=True),
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("governance_policy_id", sa.String(length=36), nullable=True),
        sa.Column("governance_status", sa.String(length=16), nullable=True),
        sa.Column("governance_reason", sa.String(length=128), nullable=True),
        sa.Column("governance_not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_token_reservation", sa.BigInteger(), nullable=True),
        sa.Column("lifetime_request_budget", sa.BigInteger(), nullable=True),
        sa.Column("lifetime_token_budget", sa.BigInteger(), nullable=True),
        sa.Column("lifetime_cost_budget_usd", sa.Numeric(20, 8), nullable=True),
    ):
        op.add_column("evaluation_runs", column)

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE evaluation_runs SET "
            "failed_attempt_count = CASE "
            "WHEN status = 'pending' THEN "
            "CASE WHEN attempt_count < max_attempts THEN attempt_count ELSE max_attempts END "
            "WHEN status = 'running' THEN "
            "CASE WHEN attempt_count <= 1 THEN 0 "
            "WHEN attempt_count - 1 < max_attempts THEN attempt_count - 1 "
            "ELSE max_attempts END "
            "WHEN status = 'failed' THEN "
            "CASE WHEN attempt_count < max_attempts THEN attempt_count ELSE max_attempts END "
            "ELSE 0 END, "
            "dispatch_count = 0, governance_status = 'legacy_unmanaged'"
        )
    )

    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.drop_constraint(op.f("ck_evaluation_runs_attempt_within_limit"), type_="check")
        batch_op.alter_column("failed_attempt_count", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("dispatch_count", existing_type=sa.BigInteger(), nullable=False)
        batch_op.alter_column(
            "governance_status", existing_type=sa.String(length=16), nullable=False
        )
        batch_op.create_foreign_key(
            op.f("fk_evaluation_runs_governance_policy_id_governance_policies"),
            "governance_policies",
            ["governance_policy_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_failed_attempt_count_nonnegative"),
            "failed_attempt_count >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_failed_attempt_count_within_limit"),
            "failed_attempt_count <= max_attempts",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_dispatch_count_nonnegative"), "dispatch_count >= 0"
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_input_token_reservation_nonnegative"),
            "input_token_reservation IS NULL OR input_token_reservation >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_lifetime_request_budget_nonnegative"),
            "lifetime_request_budget IS NULL OR lifetime_request_budget >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_lifetime_token_budget_nonnegative"),
            "lifetime_token_budget IS NULL OR lifetime_token_budget >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_lifetime_cost_budget_nonnegative"),
            "lifetime_cost_budget_usd IS NULL OR lifetime_cost_budget_usd >= 0",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_governance_status_values"),
            "governance_status IN ('legacy_unmanaged', 'managed', 'delayed', 'exhausted')",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_governance_policy_matches_status"),
            "(governance_status = 'legacy_unmanaged' AND governance_policy_id IS NULL) OR "
            "(governance_status <> 'legacy_unmanaged' AND governance_policy_id IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_governance_delay_matches_pending"),
            "governance_not_before IS NULL OR "
            "(status = 'pending' AND governance_status = 'delayed')",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_governance_exhausted_is_failed"),
            "governance_status <> 'exhausted' OR status = 'failed'",
        )

    op.create_index(
        "ix_evaluation_runs_governance_policy_id",
        "evaluation_runs",
        ["governance_policy_id"],
        unique=False,
    )
    op.create_index(
        "ix_evaluation_runs_governance_dispatch",
        "evaluation_runs",
        [
            "status",
            "governance_status",
            "governance_not_before",
            "last_scheduled_at",
            "created_at",
        ],
        unique=False,
    )
    op.create_index(
        "ix_evaluation_runs_started_at_id",
        "evaluation_runs",
        ["started_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_evaluation_runs_finished_at_id",
        "evaluation_runs",
        ["finished_at", "id"],
        unique=False,
    )


def _add_response_provider_metadata() -> None:
    for column in (
        sa.Column("provider_request_id", sa.String(length=256), nullable=True),
        sa.Column("returned_model", sa.String(length=256), nullable=True),
        sa.Column("system_fingerprint", sa.String(length=256), nullable=True),
        sa.Column("finish_reason", sa.String(length=128), nullable=True),
        sa.Column("http_attempt_count", sa.Integer(), nullable=True),
    ):
        op.add_column("evaluation_responses", column)
    with op.batch_alter_table("evaluation_responses") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_evaluation_responses_http_attempt_count_positive"),
            "http_attempt_count IS NULL OR http_attempt_count >= 1",
        )


def _create_minute_bucket_table() -> None:
    table = "governance_minute_buckets"
    columns: list[sa.SchemaItem] = [
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_id", sa.String(length=36), nullable=False),
        sa.Column("policy_id", sa.String(length=36), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_requests", sa.BigInteger(), nullable=False),
        sa.Column("reserved_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("consumed_requests", sa.BigInteger(), nullable=False),
        sa.Column("consumed_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("consumed_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        _fk(table, "scope_id", "governance_scopes"),
        _fk(table, "policy_id", "governance_policies"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governance_minute_buckets")),
        sa.UniqueConstraint(
            "scope_id",
            "policy_id",
            "window_start",
            name=op.f("uq_governance_minute_buckets_scope_policy_window"),
        ),
    ]
    for column in (
        "reserved_requests",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "consumed_requests",
        "consumed_input_tokens",
        "consumed_output_tokens",
    ):
        columns.append(_check(table, f"{column}_nonnegative", f"{column} >= 0"))
    op.create_table(table, *columns)
    op.create_index(
        "ix_governance_minute_buckets_window",
        table,
        ["window_start", "scope_id"],
        unique=False,
    )


def _create_question_execution_table() -> None:
    table = "question_executions"
    op.create_table(
        table,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("question_id", sa.String(length=36), nullable=False),
        sa.Column("execution_generation", sa.Integer(), nullable=False),
        sa.Column("next_provider_attempt", sa.Integer(), nullable=False),
        sa.Column("first_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        _check(table, "generation_nonnegative", "execution_generation >= 0"),
        _check(table, "next_attempt_positive", "next_provider_attempt >= 1"),
        _fk(table, "run_id", "evaluation_runs"),
        _fk(table, "question_id", "questions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_question_executions")),
        sa.UniqueConstraint(
            "run_id", "question_id", name=op.f("uq_question_executions_run_question")
        ),
    )
    op.create_index("ix_question_executions_run_id", table, ["run_id"], unique=False)
    op.create_index("ix_question_executions_question_id", table, ["question_id"], unique=False)
    op.create_index(
        "ix_question_executions_retry_due",
        table,
        ["retry_not_before", "run_id"],
        unique=False,
    )


def _create_provider_call_reservation_table() -> None:
    table = "provider_call_reservations"
    op.create_table(
        table,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("operation_key", sa.String(length=255), nullable=False),
        sa.Column("policy_id", sa.String(length=36), nullable=False),
        sa.Column("question_execution_id", sa.String(length=36), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("question_id", sa.String(length=36), nullable=True),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("global_scope_id", sa.String(length=36), nullable=False),
        sa.Column("provider_scope_id", sa.String(length=36), nullable=False),
        sa.Column("model_scope_id", sa.String(length=36), nullable=False),
        sa.Column("run_scope_id", sa.String(length=36), nullable=True),
        sa.Column("execution_generation", sa.Integer(), nullable=False),
        sa.Column("provider_attempt", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("reserved_output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("reserved_cost_usd", sa.Numeric(20, 8), nullable=True),
        sa.Column("actual_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("actual_output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("actual_cost_usd", sa.Numeric(20, 8), nullable=True),
        sa.Column("outcome_code", sa.String(length=128), nullable=True),
        sa.Column("send_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        _check(
            table,
            "state_values",
            "state IN ('reserved', 'send_started', 'settled_actual', "
            "'settled_conservative', 'released_pre_send')",
        ),
        _check(table, "generation_nonnegative", "execution_generation >= 0"),
        _check(table, "provider_attempt_positive", "provider_attempt >= 1"),
        _check(table, "lease_token_nonnegative", "lease_token IS NULL OR lease_token >= 0"),
        _check(
            table,
            "reserved_input_tokens_nonnegative",
            "reserved_input_tokens IS NULL OR reserved_input_tokens >= 0",
        ),
        _check(
            table,
            "reserved_output_nonnegative",
            "reserved_output_tokens IS NULL OR reserved_output_tokens >= 0",
        ),
        _check(
            table,
            "reserved_cost_nonnegative",
            "reserved_cost_usd IS NULL OR reserved_cost_usd >= 0",
        ),
        _check(
            table,
            "actual_input_tokens_nonnegative",
            "actual_input_tokens IS NULL OR actual_input_tokens >= 0",
        ),
        _check(
            table,
            "actual_output_tokens_nonnegative",
            "actual_output_tokens IS NULL OR actual_output_tokens >= 0",
        ),
        _check(
            table,
            "actual_cost_nonnegative",
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
        ),
        _check(
            table,
            "state_timestamps_coherent",
            "(state = 'reserved' AND send_started_at IS NULL AND settled_at IS NULL) OR "
            "(state = 'send_started' AND send_started_at IS NOT NULL AND settled_at IS NULL) OR "
            "(state = 'released_pre_send' AND send_started_at IS NULL "
            "AND settled_at IS NOT NULL) OR "
            "(state IN ('settled_actual', 'settled_conservative') "
            "AND send_started_at IS NOT NULL AND settled_at IS NOT NULL)",
        ),
        _check(
            table,
            "question_execution_coherent",
            "(question_execution_id IS NULL AND run_id IS NULL AND question_id IS NULL "
            "AND run_scope_id IS NULL) OR "
            "(question_execution_id IS NOT NULL AND run_id IS NOT NULL "
            "AND question_id IS NOT NULL AND run_scope_id IS NOT NULL)",
        ),
        _fk(table, "policy_id", "governance_policies"),
        _fk(table, "question_execution_id", "question_executions"),
        _fk(table, "run_id", "evaluation_runs"),
        _fk(table, "question_id", "questions"),
        _fk(table, "model_id", "models"),
        _fk(table, "global_scope_id", "governance_scopes"),
        _fk(table, "provider_scope_id", "governance_scopes"),
        _fk(table, "model_scope_id", "governance_scopes"),
        _fk(table, "run_scope_id", "governance_scopes"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_call_reservations")),
        sa.UniqueConstraint(
            "operation_key", name=op.f("uq_provider_call_reservations_operation_key")
        ),
        sa.UniqueConstraint(
            "run_id",
            "question_id",
            "execution_generation",
            "provider_attempt",
            name=op.f("uq_provider_call_reservations_logical_attempt"),
        ),
    )
    op.create_index("ix_provider_call_reservations_run_id", table, ["run_id"], unique=False)
    op.create_index(
        "ix_provider_call_reservations_question_id", table, ["question_id"], unique=False
    )
    op.create_index("ix_provider_call_reservations_model_id", table, ["model_id"], unique=False)
    op.create_index(
        "ix_provider_call_reservations_state_lease",
        table,
        ["state", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_provider_call_reservations_run_created",
        table,
        ["run_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_provider_call_reservations_provider_scope",
        table,
        ["provider_scope_id", "state"],
        unique=False,
    )


def _create_audit_event_table() -> None:
    table = "audit_events"
    op.create_table(
        table,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("retention_class", sa.String(length=11), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("model_id", sa.String(length=36), nullable=True),
        sa.Column("question_id", sa.String(length=36), nullable=True),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("reservation_id", sa.String(length=36), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("provider_attempt", sa.Integer(), nullable=True),
        sa.Column("lease_token", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        _check(
            table,
            "retention_class_values",
            "retention_class IN ('operational', 'security')",
        ),
        _check(table, "payload_hash_length", "length(payload_hash) = 64"),
        _check(table, "attempt_nonnegative", "attempt IS NULL OR attempt >= 0"),
        _check(
            table,
            "provider_attempt_positive",
            "provider_attempt IS NULL OR provider_attempt >= 1",
        ),
        _check(table, "lease_token_nonnegative", "lease_token IS NULL OR lease_token >= 0"),
        _check(table, "duration_nonnegative", "duration_ms IS NULL OR duration_ms >= 0"),
        _check(table, "expiry_after_occurrence", "expires_at > occurred_at"),
        _fk(table, "reservation_id", "provider_call_reservations"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
        sa.UniqueConstraint("event_key", name=op.f("uq_audit_events_event_key")),
    )
    op.create_index(
        "ix_audit_events_run_occurred",
        table,
        ["run_id", "occurred_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_type_occurred",
        table,
        ["event_type", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_expiry",
        table,
        ["expires_at", "retention_class"],
        unique=False,
    )


def upgrade() -> None:
    # A policy is intentionally not seeded here. Application bootstrap creates a
    # deterministic default without violating the importer's empty-target contract.
    _create_governance_policy_table()
    _create_governance_scope_table()
    _add_run_governance_columns()
    _add_response_provider_metadata()
    _create_minute_bucket_table()
    _create_question_execution_table()
    _create_provider_call_reservation_table()
    _create_audit_event_table()


def _ensure_downgrade_is_lossless() -> None:
    connection = op.get_bind()
    for table in (
        "governance_policies",
        "governance_scopes",
        "governance_minute_buckets",
        "question_executions",
        "provider_call_reservations",
        "audit_events",
    ):
        count = connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                "Cannot downgrade governance schema while ledger, audit, policy, scope, "
                "bucket, or question-execution evidence exists; archive and reconcile it first"
            )

    run_evidence = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM evaluation_runs WHERE "
            "governance_policy_id IS NOT NULL OR governance_status <> 'legacy_unmanaged' OR "
            "governance_reason IS NOT NULL OR governance_not_before IS NOT NULL OR "
            "failed_attempt_count <> 0 OR dispatch_count <> 0 OR last_scheduled_at IS NOT NULL OR "
            "input_token_reservation IS NOT NULL OR lifetime_request_budget IS NOT NULL OR "
            "lifetime_token_budget IS NOT NULL OR lifetime_cost_budget_usd IS NOT NULL OR "
            "attempt_count > max_attempts"
        )
    ).scalar_one()
    response_evidence = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM evaluation_responses WHERE provider_request_id IS NOT NULL OR "
            "returned_model IS NOT NULL OR system_fingerprint IS NOT NULL OR "
            "finish_reason IS NOT NULL OR http_attempt_count IS NOT NULL"
        )
    ).scalar_one()
    if run_evidence or response_evidence:
        raise RuntimeError(
            "Cannot downgrade governance schema while Run governance/failure evidence or "
            "Response Provider metadata exists; archive and reconcile it first"
        )


def downgrade() -> None:
    # This guard must remain before the first DDL operation.
    _ensure_downgrade_is_lossless()

    op.drop_table("audit_events")
    op.drop_table("provider_call_reservations")
    op.drop_table("question_executions")
    op.drop_table("governance_minute_buckets")

    with op.batch_alter_table("evaluation_responses") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_evaluation_responses_http_attempt_count_positive"), type_="check"
        )
        batch_op.drop_column("http_attempt_count")
        batch_op.drop_column("finish_reason")
        batch_op.drop_column("system_fingerprint")
        batch_op.drop_column("returned_model")
        batch_op.drop_column("provider_request_id")

    op.drop_index("ix_evaluation_runs_finished_at_id", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_started_at_id", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_governance_dispatch", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_governance_policy_id", table_name="evaluation_runs")
    with op.batch_alter_table("evaluation_runs") as batch_op:
        for constraint in (
            "governance_exhausted_is_failed",
            "governance_delay_matches_pending",
            "governance_policy_matches_status",
            "governance_status_values",
            "lifetime_cost_budget_nonnegative",
            "lifetime_token_budget_nonnegative",
            "lifetime_request_budget_nonnegative",
            "input_token_reservation_nonnegative",
            "dispatch_count_nonnegative",
            "failed_attempt_count_within_limit",
            "failed_attempt_count_nonnegative",
        ):
            batch_op.drop_constraint(op.f(f"ck_evaluation_runs_{constraint}"), type_="check")
        batch_op.drop_constraint(
            op.f("fk_evaluation_runs_governance_policy_id_governance_policies"),
            type_="foreignkey",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_attempt_within_limit"),
            "attempt_count <= max_attempts",
        )
        batch_op.drop_column("lifetime_cost_budget_usd")
        batch_op.drop_column("lifetime_token_budget")
        batch_op.drop_column("lifetime_request_budget")
        batch_op.drop_column("input_token_reservation")
        batch_op.drop_column("governance_not_before")
        batch_op.drop_column("governance_reason")
        batch_op.drop_column("governance_status")
        batch_op.drop_column("governance_policy_id")
        batch_op.drop_column("last_scheduled_at")
        batch_op.drop_column("dispatch_count")
        batch_op.drop_column("failed_attempt_count")

    op.drop_table("governance_scopes")
    op.drop_table("governance_policies")
