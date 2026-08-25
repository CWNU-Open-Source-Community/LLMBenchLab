"""Add durable task execution metadata and lease invariants.

Revision ID: 20260825_0002
Revises: 20260824_0001
Create Date: 2026-08-25 04:00:00 UTC
"""

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa

from alembic import op

revision: str = "20260825_0002"
down_revision: str | None = "20260824_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RELIABILITY_CHECKS = (
    "attempt_count_nonnegative",
    "max_attempts_positive",
    "attempt_within_limit",
    "lease_token_nonnegative",
    "lease_matches_running_status",
    "next_attempt_only_pending",
    "dead_letter_only_failed",
)


def _settle_interrupted_phase1_runs(connection: sa.Connection) -> None:
    """Preserve Phase 1's non-resumable snapshot contract at the upgrade boundary."""

    runs = sa.table(
        "evaluation_runs",
        sa.column("id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("total_questions", sa.Integer()),
        sa.column("completed_questions", sa.Integer()),
        sa.column("correct_questions", sa.Integer()),
        sa.column("error_questions", sa.Integer()),
        sa.column("score", sa.Float()),
        sa.column("completion_rate", sa.Float()),
        sa.column("answered_accuracy", sa.Float()),
        sa.column("average_latency_ms", sa.Float()),
        sa.column("input_tokens", sa.Integer()),
        sa.column("output_tokens", sa.Integer()),
        sa.column("estimated_cost", sa.Numeric(20, 8)),
        sa.column("cancellation_requested", sa.Boolean()),
        sa.column("finished_at", sa.DateTime(timezone=True)),
        sa.column("error_message", sa.Text()),
        sa.column("last_error", sa.Text()),
    )
    responses = sa.table(
        "evaluation_responses",
        sa.column("run_id", sa.String()),
        sa.column("raw_response", sa.Text()),
        sa.column("score", sa.Float()),
        sa.column("latency_ms", sa.Float()),
        sa.column("input_tokens", sa.Integer()),
        sa.column("output_tokens", sa.Integer()),
        sa.column("estimated_cost", sa.Numeric(20, 8)),
        sa.column("error_type", sa.String()),
    )
    interrupted = connection.execute(
        sa.select(runs.c.id, runs.c.total_questions, runs.c.cancellation_requested).where(
            runs.c.status == "running"
        )
    ).all()
    for run_id, total_questions, cancellation_requested in interrupted:
        evidence = connection.execute(
            sa.select(
                responses.c.raw_response,
                responses.c.score,
                responses.c.latency_ms,
                responses.c.input_tokens,
                responses.c.output_tokens,
                responses.c.estimated_cost,
                responses.c.error_type,
            ).where(responses.c.run_id == run_id)
        ).all()
        response_count = len(evidence)
        score_sum = sum(float(row.score or 0) for row in evidence)
        completed_outputs = sum(
            row.raw_response is not None and row.raw_response != "" for row in evidence
        )
        evaluable = sum(
            row.error_type is None and row.raw_response is not None and row.raw_response != ""
            for row in evidence
        )
        errors = sum(row.error_type is not None for row in evidence)
        latencies = [float(row.latency_ms) for row in evidence if row.latency_ms is not None]
        input_reports = [row.input_tokens for row in evidence if row.input_tokens is not None]
        output_reports = [row.output_tokens for row in evidence if row.output_tokens is not None]
        cost_reports = [
            Decimal(str(row.estimated_cost)) for row in evidence if row.estimated_cost is not None
        ]
        correct = round(score_sum)
        planned = int(total_questions)
        was_cancelled = bool(cancellation_requested)
        connection.execute(
            runs.update()
            .where(runs.c.id == run_id)
            .values(
                status="cancelled" if was_cancelled else "failed",
                completed_questions=response_count,
                correct_questions=correct,
                error_questions=errors,
                score=(score_sum / planned * 100) if planned else 0.0,
                completion_rate=(completed_outputs / planned * 100) if planned else 0.0,
                answered_accuracy=(correct / evaluable * 100) if evaluable else None,
                average_latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
                input_tokens=(
                    sum(input_reports)
                    if response_count and len(input_reports) == response_count
                    else None
                ),
                output_tokens=(
                    sum(output_reports)
                    if response_count and len(output_reports) == response_count
                    else None
                ),
                estimated_cost=(
                    sum(cost_reports, Decimal(0))
                    if response_count and len(cost_reports) == response_count
                    else None
                ),
                finished_at=sa.func.current_timestamp(),
                error_message=None if was_cancelled else "interrupted_by_reliability_migration",
                last_error=(
                    "migrated_cancelled_run" if was_cancelled else "migrated_interrupted_run"
                ),
            )
        )


def upgrade() -> None:
    inconsistent_active_runs = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM evaluation_runs AS run "
                "WHERE run.status IN ('pending', 'running') "
                "AND run.completed_questions <> ("
                "SELECT COUNT(*) FROM evaluation_responses AS response "
                "WHERE response.run_id = run.id)"
            )
        )
        .scalar_one()
    )
    if inconsistent_active_runs:
        raise RuntimeError(
            "Active run progress does not match persisted responses; repair the evidence "
            "before enabling reliable execution"
        )

    op.add_column("evaluation_runs", sa.Column("attempt_count", sa.Integer(), nullable=True))
    op.add_column("evaluation_runs", sa.Column("max_attempts", sa.Integer(), nullable=True))
    op.add_column("evaluation_runs", sa.Column("lease_owner", sa.String(length=128), nullable=True))
    op.add_column("evaluation_runs", sa.Column("lease_token", sa.BigInteger(), nullable=True))
    op.add_column(
        "evaluation_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "evaluation_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "evaluation_runs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "evaluation_runs", sa.Column("last_enqueued_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("evaluation_runs", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "evaluation_runs", sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True)
    )

    connection = op.get_bind()
    connection.execute(
        sa.text("UPDATE evaluation_runs SET attempt_count = 0, max_attempts = 3, lease_token = 0")
    )
    _settle_interrupted_phase1_runs(connection)

    with op.batch_alter_table("evaluation_runs") as batch_op:
        batch_op.alter_column("attempt_count", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("max_attempts", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("lease_token", existing_type=sa.BigInteger(), nullable=False)
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_attempt_count_nonnegative"), "attempt_count >= 0"
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_max_attempts_positive"), "max_attempts >= 1"
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_attempt_within_limit"),
            "attempt_count <= max_attempts",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_lease_token_nonnegative"), "lease_token >= 0"
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_lease_matches_running_status"),
            "(status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL) OR "
            "(status <> 'running' AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL AND heartbeat_at IS NULL)",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_next_attempt_only_pending"),
            "next_attempt_at IS NULL OR status = 'pending'",
        )
        batch_op.create_check_constraint(
            op.f("ck_evaluation_runs_dead_letter_only_failed"),
            "dead_lettered_at IS NULL OR status = 'failed'",
        )

    op.create_index(
        "ix_evaluation_runs_dispatch_due",
        "evaluation_runs",
        ["status", "cancellation_requested", "next_attempt_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_evaluation_runs_lease_expiry",
        "evaluation_runs",
        ["status", "cancellation_requested", "lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    active_runs = (
        op.get_bind()
        .execute(
            sa.text("SELECT COUNT(*) FROM evaluation_runs WHERE status IN ('pending', 'running')")
        )
        .scalar_one()
    )
    if active_runs:
        raise RuntimeError(
            "Cannot downgrade reliable execution metadata while pending or running runs exist; "
            "drain, cancel, or fail active runs first"
        )

    op.drop_index("ix_evaluation_runs_lease_expiry", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_dispatch_due", table_name="evaluation_runs")
    with op.batch_alter_table("evaluation_runs") as batch_op:
        for constraint_name in reversed(_RELIABILITY_CHECKS):
            batch_op.drop_constraint(op.f(f"ck_evaluation_runs_{constraint_name}"), type_="check")
        batch_op.drop_column("dead_lettered_at")
        batch_op.drop_column("last_error")
        batch_op.drop_column("last_enqueued_at")
        batch_op.drop_column("next_attempt_at")
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_token")
        batch_op.drop_column("lease_owner")
        batch_op.drop_column("max_attempts")
        batch_op.drop_column("attempt_count")
