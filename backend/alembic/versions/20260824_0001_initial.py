"""Normalize the legacy bootstrap schema to the Phase 1 schema.

Revision ID: 20260824_0001
Revises: 20260824_0000
Create Date: 2026-08-24 00:00:01 UTC
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260824_0001"
down_revision: str | None = "20260824_0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_question_positions() -> None:
    """Recover each legacy dataset's insertion order as a zero-based position."""

    connection = op.get_bind()
    order_expression = (
        "benchmark_id, rowid" if connection.dialect.name == "sqlite" else "benchmark_id, id"
    )
    rows = connection.execute(
        sa.text(f"SELECT id, benchmark_id FROM questions ORDER BY {order_expression}")
    ).mappings()
    next_position: dict[str, int] = {}
    for row in rows:
        benchmark_id = str(row["benchmark_id"])
        position = next_position.get(benchmark_id, 0)
        connection.execute(
            sa.text("UPDATE questions SET position = :position WHERE id = :question_id"),
            {"position": position, "question_id": row["id"]},
        )
        next_position[benchmark_id] = position + 1


def _validate_legacy_model_rows() -> None:
    invalid_models = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM models WHERE "
                "(provider_type = 'openai_compatible' AND "
                "(base_url IS NULL OR remote_model_name IS NULL OR api_key_env IS NULL)) OR "
                "(provider_type = 'mock' AND "
                "(base_url IS NOT NULL OR remote_model_name IS NOT NULL "
                "OR api_key_env IS NOT NULL))"
            )
        )
        .scalar_one()
    )
    if invalid_models:
        raise RuntimeError(
            "Legacy model rows violate the provider configuration constraints; "
            "correct the rows before upgrading"
        )


def _validate_legacy_question_table() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return
    question_options = next(
        (
            row
            for row in connection.exec_driver_sql("PRAGMA table_list").mappings()
            if row["schema"] == "main" and row["name"] == "questions"
        ),
        None,
    )
    if question_options is None or bool(question_options.get("wr", 0)):
        raise RuntimeError(
            "The legacy questions table must support SQLite rowid to recover dataset order"
        )


def upgrade() -> None:
    # Validate before SQLite batch mode creates any temporary replacement table.
    _validate_legacy_model_rows()
    _validate_legacy_question_table()
    with op.batch_alter_table("models") as batch_op:
        batch_op.alter_column(
            "input_price_per_million",
            existing_type=sa.Numeric(precision=20, scale=8),
            nullable=True,
        )
        batch_op.alter_column(
            "output_price_per_million",
            existing_type=sa.Numeric(precision=20, scale=8),
            nullable=True,
        )
        batch_op.create_check_constraint(
            op.f("ck_models_openai_configuration_required"),
            "provider_type != 'openai_compatible' OR "
            "(base_url IS NOT NULL AND remote_model_name IS NOT NULL AND api_key_env IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            op.f("ck_models_mock_configuration_empty"),
            "provider_type != 'mock' OR "
            "(base_url IS NULL AND remote_model_name IS NULL AND api_key_env IS NULL)",
        )

    op.add_column("questions", sa.Column("position", sa.Integer(), nullable=True))
    _backfill_question_positions()
    with op.batch_alter_table("questions") as batch_op:
        batch_op.alter_column("position", existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint(
            op.f("uq_questions_benchmark_position"), ["benchmark_id", "position"]
        )


def downgrade() -> None:
    with op.batch_alter_table("questions") as batch_op:
        batch_op.drop_constraint(op.f("uq_questions_benchmark_position"), type_="unique")
        batch_op.drop_column("position")

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE models SET input_price_per_million = 0 WHERE input_price_per_million IS NULL"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE models SET output_price_per_million = 0 WHERE output_price_per_million IS NULL"
        )
    )
    with op.batch_alter_table("models") as batch_op:
        batch_op.drop_constraint(op.f("ck_models_mock_configuration_empty"), type_="check")
        batch_op.drop_constraint(op.f("ck_models_openai_configuration_required"), type_="check")
        batch_op.alter_column(
            "output_price_per_million",
            existing_type=sa.Numeric(precision=20, scale=8),
            nullable=False,
        )
        batch_op.alter_column(
            "input_price_per_million",
            existing_type=sa.Numeric(precision=20, scale=8),
            nullable=False,
        )
