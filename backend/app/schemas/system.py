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
    managed_backlog: int
    governance_delayed: int
    governance_exhausted: int
    active_provider_attempts: int
    overdrawn_governance_scopes: int
    total_attempts: int
    total_failed_attempts: int
    total_dispatches: int
    timestamp: datetime


class TaskEventCounts(ORMModel):
    total: int
    governance_policy_bootstrapped: int
    governance_policy_applied: int
    run_admitted: int
    run_claimed: int
    run_cancel_requested: int
    run_deferred: int
    run_yielded: int
    run_terminal: int
    run_retry_scheduled: int
    run_dead_lettered: int
    run_lease_reconciled: int
    provider_attempt_reserved: int
    provider_attempt_send_started: int
    provider_attempt_settled: int
    question_evidence_persisted: int
    queue_notification: int
    governance_integrity_error: int


class TaskLatencyPercentiles(ORMModel):
    sample_count: int
    truncated: bool
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None


class TaskHistoryResponse(ORMModel):
    window_start: datetime
    window_end: datetime
    window_hours: int
    event_counts: TaskEventCounts
    queue_latency: TaskLatencyPercentiles
    execution_latency: TaskLatencyPercentiles
    end_to_end_latency: TaskLatencyPercentiles
    latency_sample_limit: int
    timestamp: datetime


class InfoResponse(ORMModel):
    name: str
    version: str
    api_version: str
    protocol_version: str
    environment: str
    capabilities: dict[str, list[str] | str]
