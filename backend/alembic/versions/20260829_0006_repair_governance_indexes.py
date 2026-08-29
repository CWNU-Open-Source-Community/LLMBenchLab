"""Repair three governance indexes missing from an early revision 0004 schema.

Revision ID: 20260829_0006
Revises: 20260828_0005
Create Date: 2026-08-29 00:00:00 UTC
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "20260829_0006"
down_revision: str | None = "20260828_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalized_predicate(value: object) -> str:
    predicate = " ".join(str(value).strip().lower().split())
    while predicate.startswith("(") and predicate.endswith(")"):
        predicate = predicate[1:-1].strip()
    return predicate


def _ensure_index(
    table_name: str,
    index_name: str,
    columns: tuple[str, ...],
    *,
    unique: bool,
    sqlite_where: sa.TextClause | None = None,
    postgresql_where: sa.TextClause | None = None,
) -> None:
    connection = op.get_bind()
    existing_indexes: dict[str, dict[str, Any]] = {
        str(index["name"]): index for index in sa.inspect(connection).get_indexes(table_name)
    }
    existing = existing_indexes.get(index_name)
    if existing is None:
        op.create_index(
            index_name,
            table_name,
            list(columns),
            unique=unique,
            sqlite_where=sqlite_where,
            postgresql_where=postgresql_where,
        )
        return

    actual_columns = tuple(str(column) for column in existing.get("column_names", ()))
    if actual_columns != columns or bool(existing.get("unique")) is not unique:
        raise RuntimeError(
            f"Existing index {index_name} does not match the repair migration definition"
        )

    expected_predicate: sa.TextClause | None = None
    reflected_predicate: object | None = None
    dialect_options = existing.get("dialect_options", {})
    if connection.dialect.name == "sqlite":
        expected_predicate = sqlite_where
        reflected_predicate = dialect_options.get("sqlite_where")
    elif connection.dialect.name == "postgresql":
        expected_predicate = postgresql_where
        reflected_predicate = dialect_options.get("postgresql_where")
    predicate_mismatch = (expected_predicate is None) != (reflected_predicate is None)
    if expected_predicate is not None and reflected_predicate is not None:
        predicate_mismatch = predicate_mismatch or (
            _normalized_predicate(reflected_predicate) != _normalized_predicate(expected_predicate)
        )
    if predicate_mismatch:
        raise RuntimeError(
            f"Existing index {index_name} does not match the repair migration predicate"
        )


def _ensure_active_policy_is_unique() -> None:
    policies = sa.table(
        "governance_policies",
        sa.column("is_active", sa.Boolean()),
    )
    active_policy_count = (
        op.get_bind()
        .execute(
            sa.select(sa.func.count()).select_from(policies).where(policies.c.is_active.is_(True))
        )
        .scalar_one()
    )
    if active_policy_count > 1:
        raise RuntimeError("Cannot repair governance indexes while multiple active policies exist")


def upgrade() -> None:
    # This data guard must run before the first repair DDL. In particular, a
    # direct Alembic invocation must not leave the two non-unique indexes behind
    # when restoring the partial unique policy index would be lossy.
    _ensure_active_policy_is_unique()
    _ensure_index(
        "evaluation_runs",
        "ix_evaluation_runs_started_at_id",
        ("started_at", "id"),
        unique=False,
    )
    _ensure_index(
        "evaluation_runs",
        "ix_evaluation_runs_finished_at_id",
        ("finished_at", "id"),
        unique=False,
    )
    _ensure_index(
        "governance_policies",
        "uq_governance_policies_single_active",
        ("is_active",),
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
        postgresql_where=sa.text("is_active IS TRUE"),
    )


def downgrade() -> None:
    """Retain indexes that are already part of the canonical revision 0005 schema."""

    return None
