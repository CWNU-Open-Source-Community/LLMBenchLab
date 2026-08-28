"""Database-time facts for one long-running Worker process generation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime


class WorkerProcess(Base):
    """Low-volume lifecycle and coalesced main-loop progress facts."""

    __tablename__ = "worker_processes"
    __table_args__ = (
        UniqueConstraint("worker_id", name="uq_worker_processes_worker_id"),
        CheckConstraint(
            "length(worker_id) >= 1 AND length(worker_id) <= 128",
            name="worker_id_length",
        ),
        CheckConstraint("last_seen_at >= started_at", name="seen_after_start"),
        *(
            CheckConstraint(
                f"{column} IS NULL OR ({column} >= started_at AND {column} <= last_seen_at)",
                name=f"{column}_within_seen",
            )
            for column in (
                "last_scan_at",
                "last_claim_at",
                "last_progress_at",
                "last_lease_heartbeat_at",
            )
        ),
        CheckConstraint(
            "stopped_at IS NULL OR stopped_at >= last_seen_at",
            name="stopped_after_seen",
        ),
        Index(
            "ix_worker_processes_stopped_seen_generation",
            "stopped_at",
            "last_seen_at",
            "generation_id",
        ),
    )

    generation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_claim_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_progress_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_lease_heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
