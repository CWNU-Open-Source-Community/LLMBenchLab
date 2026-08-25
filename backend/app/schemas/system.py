"""System endpoint schemas."""

from datetime import datetime
from typing import Literal

from app.schemas.base import ORMModel


class HealthResponse(ORMModel):
    status: Literal["ok"]
    database: Literal["ok"]
    version: str
    timestamp: datetime


class InfoResponse(ORMModel):
    name: str
    version: str
    api_version: str
    protocol_version: str
    environment: str
    capabilities: dict[str, list[str] | str]
