"""Versioned benchmark metadata."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, CheckConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UTCDateTime

if TYPE_CHECKING:
    from app.models.evaluation_run import EvaluationRun
    from app.models.question import Question


def _uuid() -> str:
    return str(uuid4())


class Benchmark(Base):
    """An immutable imported benchmark version and its canonical dataset hash."""

    __tablename__ = "benchmarks"
    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_benchmarks_slug_version"),
        CheckConstraint("question_count >= 1", name="question_count_positive"),
        Index("ix_benchmarks_dimension_language", "dimension", "language"),
        Index("ix_benchmarks_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    dimension: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(35), nullable=False, index=True)
    license: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(2048), nullable=False)
    evaluator_type: Mapped[str] = mapped_column(String(128), nullable=False)
    evaluator_config: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    prompt_template: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    schema_version: Mapped[str] = mapped_column(
        String(64), default="llmbenchlab-dataset-v1", nullable=False
    )
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    question_count: Mapped[int] = mapped_column(nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    questions: Mapped[list[Question]] = relationship(
        back_populates="benchmark",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Question.external_id",
    )
    runs: Mapped[list[EvaluationRun]] = relationship(back_populates="benchmark")
