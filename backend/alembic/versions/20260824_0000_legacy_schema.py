"""Create the legacy five-entity schema used before Alembic owned startup.

Revision ID: 20260824_0000
Revises: None
Create Date: 2026-08-24 00:00:00 UTC
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260824_0000"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "models",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "provider_type",
            sa.Enum(
                "mock",
                "openai_compatible",
                name="provider_type",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("base_url", sa.String(length=2048), nullable=True),
        sa.Column("remote_model_name", sa.String(length=256), nullable=True),
        sa.Column("api_key_env", sa.String(length=128), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "input_price_per_million",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
        ),
        sa.Column(
            "output_price_per_million",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
        ),
        sa.Column("default_parameters", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider_type IN ('mock', 'openai_compatible')",
            name=op.f("ck_models_provider_type_values"),
        ),
        sa.CheckConstraint(
            "input_price_per_million >= 0", name=op.f("ck_models_input_price_nonnegative")
        ),
        sa.CheckConstraint(
            "output_price_per_million >= 0", name=op.f("ck_models_output_price_nonnegative")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_models"),
        sa.UniqueConstraint("name", name="uq_models_name"),
    )
    op.create_index("ix_models_enabled", "models", ["enabled"])
    op.create_index("ix_models_provider_type", "models", ["provider_type"])
    op.create_index("ix_models_provider_enabled", "models", ["provider_type", "enabled"])

    op.create_table(
        "benchmarks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("dimension", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=35), nullable=False),
        sa.Column("license", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=2048), nullable=False),
        sa.Column("evaluator_type", sa.String(length=128), nullable=False),
        sa.Column("evaluator_config", sa.JSON(), nullable=False),
        sa.Column("prompt_template", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("dataset_hash", sa.String(length=64), nullable=False),
        sa.Column("question_count", sa.Integer(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "question_count >= 1", name=op.f("ck_benchmarks_question_count_positive")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_benchmarks"),
        sa.UniqueConstraint("slug", "version", name="uq_benchmarks_slug_version"),
    )
    op.create_index("ix_benchmarks_slug", "benchmarks", ["slug"])
    op.create_index("ix_benchmarks_dimension", "benchmarks", ["dimension"])
    op.create_index("ix_benchmarks_language", "benchmarks", ["language"])
    op.create_index("ix_benchmarks_dataset_hash", "benchmarks", ["dataset_hash"])
    op.create_index("ix_benchmarks_is_demo", "benchmarks", ["is_demo"])
    op.create_index("ix_benchmarks_dimension_language", "benchmarks", ["dimension", "language"])
    op.create_index("ix_benchmarks_created_at", "benchmarks", ["created_at"])

    op.create_table(
        "questions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("benchmark_id", sa.String(length=36), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column(
            "question_type",
            sa.Enum(
                "exact_match",
                "multiple_choice",
                "numeric",
                name="question_type",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("choices", sa.JSON(), nullable=True),
        sa.Column("reference_answer", sa.JSON(), nullable=False),
        sa.Column("evaluator_config", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "question_type IN ('exact_match', 'multiple_choice', 'numeric')",
            name=op.f("ck_questions_question_type_values"),
        ),
        sa.ForeignKeyConstraint(
            ["benchmark_id"],
            ["benchmarks.id"],
            name="fk_questions_benchmark_id_benchmarks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_questions"),
        sa.UniqueConstraint(
            "benchmark_id", "external_id", name="uq_questions_benchmark_external_id"
        ),
    )
    op.create_index("ix_questions_benchmark_id", "questions", ["benchmark_id"])
    op.create_index("ix_questions_benchmark_type", "questions", ["benchmark_id", "question_type"])

    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("benchmark_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "completed",
                "failed",
                "cancelled",
                name="run_status",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "protocol_version",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("model_parameters_snapshot", sa.JSON(), nullable=False),
        sa.Column("benchmark_hash_snapshot", sa.String(length=64), nullable=False),
        sa.Column("prompt_template_snapshot", sa.JSON(), nullable=False),
        sa.Column("code_commit_sha", sa.String(length=64), nullable=True),
        sa.Column("total_questions", sa.Integer(), nullable=False),
        sa.Column("completed_questions", sa.Integer(), nullable=False),
        sa.Column("correct_questions", sa.Integer(), nullable=False),
        sa.Column("error_questions", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("completion_rate", sa.Float(), nullable=True),
        sa.Column("answered_accuracy", sa.Float(), nullable=True),
        sa.Column("average_latency_ms", sa.Float(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "estimated_cost",
            sa.Numeric(precision=20, scale=8),
            nullable=True,
        ),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_evaluation_runs_status_values"),
        ),
        sa.CheckConstraint(
            "total_questions >= 0",
            name=op.f("ck_evaluation_runs_total_questions_nonnegative"),
        ),
        sa.CheckConstraint(
            "completed_questions >= 0",
            name=op.f("ck_evaluation_runs_completed_questions_nonnegative"),
        ),
        sa.CheckConstraint(
            "correct_questions >= 0",
            name=op.f("ck_evaluation_runs_correct_questions_nonnegative"),
        ),
        sa.CheckConstraint(
            "error_questions >= 0",
            name=op.f("ck_evaluation_runs_error_questions_nonnegative"),
        ),
        sa.CheckConstraint(
            "completed_questions <= total_questions",
            name=op.f("ck_evaluation_runs_completed_not_above_total"),
        ),
        sa.CheckConstraint(
            "correct_questions <= completed_questions",
            name=op.f("ck_evaluation_runs_correct_not_above_completed"),
        ),
        sa.CheckConstraint(
            "error_questions <= completed_questions",
            name=op.f("ck_evaluation_runs_errors_not_above_completed"),
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)",
            name=op.f("ck_evaluation_runs_score_range"),
        ),
        sa.CheckConstraint(
            "completion_rate IS NULL OR (completion_rate >= 0 AND completion_rate <= 100)",
            name=op.f("ck_evaluation_runs_completion_rate_range"),
        ),
        sa.CheckConstraint(
            "answered_accuracy IS NULL OR (answered_accuracy >= 0 AND answered_accuracy <= 100)",
            name=op.f("ck_evaluation_runs_answered_accuracy_range"),
        ),
        sa.CheckConstraint(
            "input_tokens >= 0", name=op.f("ck_evaluation_runs_input_tokens_nonnegative")
        ),
        sa.CheckConstraint(
            "output_tokens >= 0", name=op.f("ck_evaluation_runs_output_tokens_nonnegative")
        ),
        sa.CheckConstraint(
            "estimated_cost >= 0", name=op.f("ck_evaluation_runs_estimated_cost_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["benchmark_id"],
            ["benchmarks.id"],
            name="fk_evaluation_runs_benchmark_id_benchmarks",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name="fk_evaluation_runs_model_id_models",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_runs"),
    )
    op.create_index("ix_evaluation_runs_model_id", "evaluation_runs", ["model_id"])
    op.create_index("ix_evaluation_runs_benchmark_id", "evaluation_runs", ["benchmark_id"])
    op.create_index("ix_evaluation_runs_status", "evaluation_runs", ["status"])
    op.create_index(
        "ix_evaluation_runs_status_created", "evaluation_runs", ["status", "created_at"]
    )
    op.create_index(
        "ix_evaluation_runs_model_created", "evaluation_runs", ["model_id", "created_at"]
    )
    op.create_index(
        "ix_evaluation_runs_comparison_partition",
        "evaluation_runs",
        ["benchmark_id", "protocol_version", "benchmark_hash_snapshot"],
    )

    op.create_table(
        "evaluation_responses",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("question_id", sa.String(length=36), nullable=False),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.Column("parsed_answer", sa.JSON(), nullable=True),
        sa.Column("reference_answer_snapshot", sa.JSON(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("evaluator_name", sa.String(length=128), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "score >= 0 AND score <= 1", name=op.f("ck_evaluation_responses_score_range")
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name=op.f("ck_evaluation_responses_latency_nonnegative"),
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name=op.f("ck_evaluation_responses_input_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name=op.f("ck_evaluation_responses_output_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "estimated_cost IS NULL OR estimated_cost >= 0",
            name=op.f("ck_evaluation_responses_estimated_cost_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["questions.id"],
            name="fk_evaluation_responses_question_id_questions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["evaluation_runs.id"],
            name="fk_evaluation_responses_run_id_evaluation_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_responses"),
        sa.UniqueConstraint("run_id", "question_id", name="uq_responses_run_question"),
    )
    op.create_index("ix_evaluation_responses_run_id", "evaluation_responses", ["run_id"])
    op.create_index("ix_evaluation_responses_question_id", "evaluation_responses", ["question_id"])
    op.create_index("ix_evaluation_responses_error_type", "evaluation_responses", ["error_type"])
    op.create_index(
        "ix_evaluation_responses_run_created",
        "evaluation_responses",
        ["run_id", "created_at"],
    )
    op.create_index("ix_evaluation_responses_question", "evaluation_responses", ["question_id"])


def downgrade() -> None:
    op.drop_table("evaluation_responses")
    op.drop_table("evaluation_runs")
    op.drop_table("questions")
    op.drop_table("benchmarks")
    op.drop_table("models")
