"""A single immutable benchmark question."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import QuestionType

if TYPE_CHECKING:
    from app.models.benchmark import Benchmark
    from app.models.evaluation_response import EvaluationResponse


def _uuid() -> str:
    return str(uuid4())


class Question(Base):
    """Question content and evaluator inputs imported from a JSONL record."""

    __tablename__ = "questions"
    __table_args__ = (
        UniqueConstraint("benchmark_id", "external_id", name="uq_questions_benchmark_external_id"),
        CheckConstraint(
            "question_type IN ('exact_match', 'multiple_choice', 'numeric')",
            name="question_type_values",
        ),
        Index("ix_questions_benchmark_type", "benchmark_id", "question_type"),
        UniqueConstraint("benchmark_id", "position", name="uq_questions_benchmark_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    benchmark_id: Mapped[str] = mapped_column(
        ForeignKey("benchmarks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    position: Mapped[int] = mapped_column(nullable=False)
    question_type: Mapped[QuestionType] = mapped_column(
        Enum(
            QuestionType,
            name="question_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    choices: Mapped[dict[str, str] | None] = mapped_column(JSON)
    reference_answer: Mapped[Any] = mapped_column(JSON, nullable=False)
    evaluator_config: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    # ``metadata`` is reserved by SQLAlchemy's declarative API; keep the database
    # column name while exposing it through Pydantic as ``metadata``.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON), default=dict, nullable=False
    )

    benchmark: Mapped[Benchmark] = relationship(back_populates="questions")
    responses: Mapped[list[EvaluationResponse]] = relationship(back_populates="question")
