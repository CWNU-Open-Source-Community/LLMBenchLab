"""Shared Pydantic schema configuration and pagination metadata."""

from pydantic import BaseModel, ConfigDict, Field


class APIModel(BaseModel):
    """Strict request model used at the HTTP trust boundary."""

    model_config = ConfigDict(extra="forbid")


class ORMModel(BaseModel):
    """Response model that can validate SQLAlchemy instances."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class Pagination(APIModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)
