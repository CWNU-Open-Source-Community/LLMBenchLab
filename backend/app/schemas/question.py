"""Schemas for benchmark questions."""

from typing import Any

from pydantic import AliasChoices, Field, model_validator

from app.models.enums import QuestionType
from app.schemas.base import APIModel, ORMModel


class QuestionCreate(APIModel):
    position: int = Field(ge=0)
    external_id: str = Field(min_length=1, max_length=128)
    question_type: QuestionType
    prompt: str = Field(min_length=1, max_length=20_000)
    choices: dict[str, str] | None = None
    reference_answer: Any
    evaluator_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_question_shape(self) -> "QuestionCreate":
        if self.question_type == QuestionType.MULTIPLE_CHOICE:
            if not self.choices or len(self.choices) < 2:
                raise ValueError("multiple_choice questions require at least two choices")
            if (
                not isinstance(self.reference_answer, str)
                or self.reference_answer not in self.choices
            ):
                raise ValueError("multiple_choice reference_answer must be one of the choice keys")
        elif self.choices is not None:
            raise ValueError("choices are only valid for multiple_choice questions")
        return self


class QuestionRead(ORMModel):
    id: str
    benchmark_id: str
    external_id: str
    position: int
    question_type: QuestionType
    prompt: str
    choices: dict[str, str] | None
    reference_answer: Any
    evaluator_config: dict[str, Any]
    metadata: dict[str, Any] = Field(
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class QuestionList(ORMModel):
    items: list[QuestionRead]
    total: int
    offset: int
    limit: int
