"""System endpoint schemas."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.base import ORMModel


class HealthResponse(ORMModel):
    status: Literal["ok"]
    database: Literal["ok"]
    version: str
    timestamp: datetime


class LivenessResponse(ORMModel):
    status: Literal["live"]
    version: str
    timestamp: datetime


class ReadinessResponse(ORMModel):
    status: Literal["ready", "degraded", "not_ready"]
    database: Literal["ok", "unavailable"]
    schema_status: Literal["ok", "not_ready", "unavailable"] = Field(
        validation_alias="schema",
        serialization_alias="schema",
    )
    queue: Literal["ok", "unavailable", "disabled"]
    accepting_runs: bool
    database_reconciliation: Literal["available", "unavailable"]
    errors: list[str]
    version: str
    timestamp: datetime


class TaskMetricsResponse(ORMModel):
    pending: int
    due_pending: int
    running: int
    expired_running: int
    active_cancellation_requests: int
    retry_scheduled: int
    dead_lettered: int
    runs_with_queue_notification_error: int
    total_attempts: int
    timestamp: datetime


class InfoResponse(ORMModel):
    name: str
    version: str
    api_version: str
    protocol_version: str
    environment: str
    capabilities: dict[str, list[str] | str]
