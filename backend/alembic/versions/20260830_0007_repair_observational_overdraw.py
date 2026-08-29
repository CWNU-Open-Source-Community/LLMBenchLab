"""Repair overdraw flags created from observational input-token estimates.

Revision ID: 20260830_0007
Revises: 20260829_0006
Create Date: 2026-08-30 00:00:00 UTC
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_0007"
down_revision: str | None = "20260829_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_STATES = ("reserved", "send_started")
_SETTLED_STATES = ("settled_actual", "settled_conservative")


def _tables() -> tuple[sa.TableClause, sa.TableClause, sa.TableClause]:
    scopes = sa.table(
        "governance_scopes",
        sa.column("id", sa.String()),
        sa.column("overdrawn", sa.Boolean()),
    )
    reservations = sa.table(
        "provider_call_reservations",
        sa.column("state", sa.String()),
        sa.column("run_id", sa.String()),
        sa.column("global_scope_id", sa.String()),
        sa.column("provider_scope_id", sa.String()),
        sa.column("model_scope_id", sa.String()),
        sa.column("run_scope_id", sa.String()),
        sa.column("reserved_input_tokens", sa.BigInteger()),
        sa.column("reserved_output_tokens", sa.BigInteger()),
        sa.column("reserved_cost_usd", sa.Numeric(20, 8)),
        sa.column("actual_input_tokens", sa.BigInteger()),
        sa.column("actual_output_tokens", sa.BigInteger()),
        sa.column("actual_cost_usd", sa.Numeric(20, 8)),
    )
    runs = sa.table(
        "evaluation_runs",
        sa.column("id", sa.String()),
        sa.column("input_token_reservation", sa.BigInteger()),
    )
    return scopes, reservations, runs


def _lock_and_require_no_active_reservations(reservations: sa.TableClause) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "LOCK TABLE governance_scopes, provider_call_reservations, evaluation_runs "
            "IN ACCESS EXCLUSIVE MODE"
        )
    active_count = connection.scalar(
        sa.select(sa.func.count())
        .select_from(reservations)
        .where(reservations.c.state.in_(_ACTIVE_STATES))
    )
    if int(active_count or 0) != 0:
        raise RuntimeError(
            "Cannot repair governance overdraw semantics while Provider reservations are active"
        )


def _overdraw_expression(
    scopes: sa.TableClause,
    reservations: sa.TableClause,
    runs: sa.TableClause,
    *,
    require_explicit_input: bool,
) -> sa.Exists:
    scope_matches = sa.or_(
        reservations.c.global_scope_id == scopes.c.id,
        reservations.c.provider_scope_id == scopes.c.id,
        reservations.c.model_scope_id == scopes.c.id,
        reservations.c.run_scope_id == scopes.c.id,
    )
    explicit_input = sa.or_(
        reservations.c.run_id.is_(None),
        sa.exists(
            sa.select(1)
            .select_from(runs)
            .where(
                runs.c.id == reservations.c.run_id,
                runs.c.input_token_reservation.is_not(None),
            )
        ),
    )
    input_overdraw = sa.and_(
        reservations.c.reserved_input_tokens.is_not(None),
        reservations.c.actual_input_tokens.is_not(None),
        reservations.c.actual_input_tokens > reservations.c.reserved_input_tokens,
    )
    cost_overdraw = sa.and_(
        reservations.c.reserved_cost_usd.is_not(None),
        reservations.c.actual_cost_usd.is_not(None),
        reservations.c.actual_cost_usd > reservations.c.reserved_cost_usd,
    )
    if require_explicit_input:
        input_overdraw = sa.and_(explicit_input, input_overdraw)
        cost_overdraw = sa.and_(explicit_input, cost_overdraw)
    output_overdraw = sa.and_(
        reservations.c.reserved_output_tokens.is_not(None),
        reservations.c.actual_output_tokens.is_not(None),
        reservations.c.actual_output_tokens > reservations.c.reserved_output_tokens,
    )
    return sa.exists(
        sa.select(1)
        .select_from(reservations)
        .where(
            reservations.c.state.in_(_SETTLED_STATES),
            scope_matches,
            sa.or_(input_overdraw, output_overdraw, cost_overdraw),
        )
    )


def _recompute_overdrawn(*, require_explicit_input: bool) -> None:
    scopes, reservations, runs = _tables()
    _lock_and_require_no_active_reservations(reservations)
    op.get_bind().execute(
        scopes.update().values(
            overdrawn=_overdraw_expression(
                scopes,
                reservations,
                runs,
                require_explicit_input=require_explicit_input,
            )
        )
    )


def upgrade() -> None:
    _recompute_overdrawn(require_explicit_input=True)


def downgrade() -> None:
    _recompute_overdrawn(require_explicit_input=False)
