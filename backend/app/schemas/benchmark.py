"""Schemas for versioned benchmarks."""

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.base import APIModel, ORMModel


class BenchmarkCreate(APIModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)
    name: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=4000)
    dimension: str = Field(min_length=1, max_length=64)
    language: str = Field(min_length=1, max_length=35)
    license: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=2048)
    evaluator_type: str = Field(min_length=1, max_length=128)
    evaluator_config: dict[str, Any] = Field(default_factory=dict)
    prompt_template: dict[str, Any] = Field(default_factory=dict)
    schema_version: str = Field(default="llmbenchlab-dataset-v1", max_length=64)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    question_count: int = Field(ge=1, le=10_000)
    is_demo: bool = False


class BenchmarkRead(ORMModel):
    id: str
    slug: str
    name: str
    version: str
    description: str
    dimension: str
    language: str
    license: str
    source: str
    evaluator_type: str
    evaluator_config: dict[str, Any]
    prompt_template: dict[str, Any]
    schema_version: str
    dataset_hash: str
    question_count: int
    is_demo: bool
    created_at: datetime


class BenchmarkList(ORMModel):
    items: list[BenchmarkRead]
    total: int
    offset: int
    limit: int
