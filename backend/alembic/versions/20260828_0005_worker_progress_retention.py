"""Add Worker progress facts and bounded audit scan indexes.

Revision ID: 20260828_0005
Revises: 20260827_0004
Create Date: 2026-08-28 08:00:00 UTC
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260828_0005"
down_revision: str | None = "20260827_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table = "worker_processes"
    op.create_table(
        table,
        sa.Column("generation_id", sa.String(length=36), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_claim_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_lease_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(worker_id) >= 1 AND length(worker_id) <= 128",
            name=op.f("ck_worker_processes_worker_id_length"),
        ),
        sa.CheckConstraint(
            "last_seen_at >= started_at",
            name=op.f("ck_worker_processes_seen_after_start"),
        ),
        *(
            sa.CheckConstraint(
                f"{column} IS NULL OR ({column} >= started_at AND {column} <= last_seen_at)",
                name=op.f(f"ck_worker_processes_{column}_within_seen"),
            )
            for column in (
                "last_scan_at",
                "last_claim_at",
                "last_progress_at",
                "last_lease_heartbeat_at",
            )
        ),
        sa.CheckConstraint(
            "stopped_at IS NULL OR stopped_at >= last_seen_at",
            name=op.f("ck_worker_processes_stopped_after_seen"),
        ),
        sa.PrimaryKeyConstraint("generation_id", name=op.f("pk_worker_processes")),
        sa.UniqueConstraint("worker_id", name=op.f("uq_worker_processes_worker_id")),
    )
    op.create_index(
        "ix_worker_processes_stopped_seen_generation",
        table,
        ["stopped_at", "last_seen_at", "generation_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_expires_id",
        "audit_events",
        ["expires_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_occurred_id",
        "audit_events",
        ["occurred_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.execute(sa.text("SELECT COUNT(*) FROM worker_processes")).scalar_one()
    if count:
        raise RuntimeError(
            "Cannot downgrade Worker progress schema while process facts exist; "
            "stop Workers, preserve required facts, and explicitly clear the table first"
        )
    op.drop_index("ix_audit_events_occurred_id", table_name="audit_events")
    op.drop_index("ix_audit_events_expires_id", table_name="audit_events")
    op.drop_index(
        "ix_worker_processes_stopped_seen_generation",
        table_name="worker_processes",
    )
    op.drop_table("worker_processes")
