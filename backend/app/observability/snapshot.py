"""Bounded database snapshots shared by JSON task APIs and Prometheus."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.governance import AuditIntegrityError, validate_audit_event_for_read
from app.models import (
    AuditEvent,
    EvaluationRun,
    GovernanceRunStatus,
    GovernanceScope,
    ProviderCallReservation,
    ProviderCallReservationState,
    RunStatus,
    WorkerProcess,
)
from app.worker_progress import WorkerProgressSnapshot, collect_worker_progress

TASK_EVENT_TYPES = (
    "governance_policy_bootstrapped",
    "governance_policy_applied",
    "run_admitted",
    "run_claimed",
    "run_cancel_requested",
    "run_deferred",
    "run_yielded",
    "run_terminal",
    "run_retry_scheduled",
    "run_dead_lettered",
    "run_lease_reconciled",
    "provider_attempt_reserved",
    "provider_attempt_send_started",
    "provider_attempt_settled",
    "question_evidence_persisted",
    "queue_notification",
    "governance_integrity_error",
)

METRICS_AUDIT_WINDOW_SECONDS = 15 * 60
METRICS_AUDIT_EVENT_LIMIT = 50_000
METRICS_LATENCY_WINDOW_SECONDS = 60 * 60
METRICS_LATENCY_SAMPLE_LIMIT = 10_000


class MetricsObservationLimitExceeded(RuntimeError):
    """Raised instead of returning a truncated rolling audit count."""


@dataclass(frozen=True, slots=True)
class LatencySnapshot:
    """One bounded Run latency distribution in milliseconds."""

    sample_count: int
    truncated: bool
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None


@dataclass(frozen=True, slots=True)
class WorkerActivityOldestSnapshot:
    """Oldest activity timestamp across currently registered generations."""

    scan_at: datetime | None
    claim_at: datetime | None
    lease_heartbeat_at: datetime | None
    progress_at: datetime | None


@dataclass(frozen=True, slots=True)
class TaskCurrentSnapshot:
    """Current database gauges at one caller-supplied database timestamp."""

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
    expired_lease_oldest_age_seconds: float
    worker: WorkerProgressSnapshot
    worker_activity_oldest: WorkerActivityOldestSnapshot
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class TaskHistorySnapshot:
    """Validated typed-event counts and three bounded Run distributions."""

    window_start: datetime
    window_end: datetime
    event_counts: dict[str, int]
    audit_events_scanned: int
    queue_latency: LatencySnapshot
    execution_latency: LatencySnapshot
    end_to_end_latency: LatencySnapshot
    latency_sample_limit: int


@dataclass(frozen=True, slots=True)
class OperationalSnapshot:
    """Fixed exporter read model collected within one database transaction."""

    current: TaskCurrentSnapshot
    history: TaskHistorySnapshot


def as_utc(value: datetime) -> datetime:
    """Normalize a driver timestamp without consulting host-local time."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def database_clock(session: Session) -> datetime:
    """Read the authoritative database clock."""

    value = session.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("database_timestamp_unavailable")
    return as_utc(value)


def configure_read_snapshot(session: Session) -> None:
    """Start the strongest portable read snapshot before the first query."""

    dialect = session.get_bind().dialect.name
    connection = session.connection()
    if dialect == "sqlite":
        # SQLite's legacy driver mode does not begin a transaction for SELECT.
        connection.exec_driver_sql("BEGIN")
    elif dialect == "postgresql":
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")


def percentile(values: Sequence[float], percentage: float) -> float:
    """Return a linearly interpolated percentile over finite caller data."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile_requires_sample")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentage / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def latency_summary(
    values: Sequence[float],
    *,
    sample_limit: int = METRICS_LATENCY_SAMPLE_LIMIT,
) -> LatencySnapshot:
    """Summarize the first stable sample page and disclose truncation."""

    if sample_limit < 1:
        raise ValueError("latency_sample_limit_must_be_positive")
    truncated = len(values) > sample_limit
    selected = list(values[:sample_limit])
    if not selected:
        return LatencySnapshot(
            sample_count=0,
            truncated=False,
            p50_ms=None,
            p95_ms=None,
            p99_ms=None,
        )
    return LatencySnapshot(
        sample_count=len(selected),
        truncated=truncated,
        p50_ms=round(percentile(selected, 50), 6),
        p95_ms=round(percentile(selected, 95), 6),
        p99_ms=round(percentile(selected, 99), 6),
    )


def _run_latency_summary(
    session: Session,
    *,
    observed_at: Any,
    started_at: Any,
    ended_at: Any,
    window_start: datetime,
    window_end: datetime,
    sample_limit: int,
) -> LatencySnapshot:
    rows = session.execute(
        select(started_at, ended_at)
        .where(
            observed_at >= window_start,
            observed_at < window_end,
            started_at.is_not(None),
            ended_at.is_not(None),
            ended_at >= started_at,
        )
        .order_by(observed_at, EvaluationRun.id)
        .limit(sample_limit + 1)
    ).all()
    values = [
        (as_utc(end_value) - as_utc(start_value)).total_seconds() * 1000
        for start_value, end_value in rows
    ]
    return latency_summary(values, sample_limit=sample_limit)


def collect_task_current(
    session: Session,
    *,
    now: datetime,
    worker_stale_seconds: float,
    worker_expected_processes: int,
) -> TaskCurrentSnapshot:
    """Collect current Run, governance, lease, and Worker aggregates."""

    now = as_utc(now)
    active_run_statuses = (RunStatus.PENDING, RunStatus.RUNNING)
    row = session.execute(
        select(
            func.coalesce(
                func.sum(case((EvaluationRun.status == RunStatus.PENDING, 1), else_=0)), 0
            ).label("pending"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            (EvaluationRun.status == RunStatus.PENDING)
                            & (
                                EvaluationRun.next_attempt_at.is_(None)
                                | (EvaluationRun.next_attempt_at <= now)
                            )
                            & (
                                EvaluationRun.governance_not_before.is_(None)
                                | (EvaluationRun.governance_not_before <= now)
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("due_pending"),
            func.coalesce(
                func.sum(case((EvaluationRun.status == RunStatus.RUNNING, 1), else_=0)), 0
            ).label("running"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            (EvaluationRun.status == RunStatus.RUNNING)
                            & (EvaluationRun.lease_expires_at <= now),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("expired_running"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            EvaluationRun.cancellation_requested.is_(True)
                            & EvaluationRun.status.in_(active_run_statuses),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("active_cancellation_requests"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            (EvaluationRun.status == RunStatus.PENDING)
                            & EvaluationRun.next_attempt_at.is_not(None),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("retry_scheduled"),
            func.coalesce(
                func.sum(case((EvaluationRun.dead_lettered_at.is_not(None), 1), else_=0)), 0
            ).label("dead_lettered"),
            func.coalesce(
                func.sum(
                    case((EvaluationRun.last_error == "queue_notification_unavailable", 1), else_=0)
                ),
                0,
            ).label("runs_with_queue_notification_error"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            EvaluationRun.status.in_(active_run_statuses)
                            & (
                                EvaluationRun.governance_status
                                != GovernanceRunStatus.LEGACY_UNMANAGED
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("managed_backlog"),
            func.coalesce(
                func.sum(
                    case(
                        (EvaluationRun.governance_status == GovernanceRunStatus.DELAYED, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("governance_delayed"),
            func.coalesce(
                func.sum(
                    case(
                        (EvaluationRun.governance_status == GovernanceRunStatus.EXHAUSTED, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("governance_exhausted"),
            func.coalesce(func.sum(EvaluationRun.attempt_count), 0).label("total_attempts"),
            func.coalesce(func.sum(EvaluationRun.failed_attempt_count), 0).label(
                "total_failed_attempts"
            ),
            func.coalesce(func.sum(EvaluationRun.dispatch_count), 0).label("total_dispatches"),
            func.min(
                case(
                    (
                        (EvaluationRun.status == RunStatus.RUNNING)
                        & (EvaluationRun.lease_expires_at <= now),
                        EvaluationRun.lease_expires_at,
                    ),
                    else_=None,
                )
            ).label("oldest_expired_lease_at"),
        )
    ).one()
    active_provider_attempts = int(
        session.scalar(
            select(func.count(ProviderCallReservation.id)).where(
                ProviderCallReservation.state.in_(
                    (
                        ProviderCallReservationState.RESERVED,
                        ProviderCallReservationState.SEND_STARTED,
                    )
                )
            )
        )
        or 0
    )
    overdrawn_governance_scopes = int(
        session.scalar(
            select(func.count(GovernanceScope.id)).where(GovernanceScope.overdrawn.is_(True))
        )
        or 0
    )
    worker = collect_worker_progress(
        session,
        now=now,
        stale_seconds=worker_stale_seconds,
        expected=worker_expected_processes,
    )
    active_worker = WorkerProcess.stopped_at.is_(None)
    worker_activity_row = session.execute(
        select(
            func.min(case((active_worker, WorkerProcess.last_scan_at), else_=None)),
            func.min(case((active_worker, WorkerProcess.last_claim_at), else_=None)),
            func.min(case((active_worker, WorkerProcess.last_lease_heartbeat_at), else_=None)),
            func.min(case((active_worker, WorkerProcess.last_progress_at), else_=None)),
        )
    ).one()
    worker_activity_oldest = WorkerActivityOldestSnapshot(
        scan_at=as_utc(worker_activity_row[0]) if worker_activity_row[0] is not None else None,
        claim_at=as_utc(worker_activity_row[1]) if worker_activity_row[1] is not None else None,
        lease_heartbeat_at=(
            as_utc(worker_activity_row[2]) if worker_activity_row[2] is not None else None
        ),
        progress_at=(
            as_utc(worker_activity_row[3]) if worker_activity_row[3] is not None else None
        ),
    )
    oldest_expired = row.oldest_expired_lease_at
    expired_age = (
        max(0.0, (now - as_utc(oldest_expired)).total_seconds())
        if isinstance(oldest_expired, datetime)
        else 0.0
    )
    return TaskCurrentSnapshot(
        pending=int(row.pending),
        due_pending=int(row.due_pending),
        running=int(row.running),
        expired_running=int(row.expired_running),
        active_cancellation_requests=int(row.active_cancellation_requests),
        retry_scheduled=int(row.retry_scheduled),
        dead_lettered=int(row.dead_lettered),
        runs_with_queue_notification_error=int(row.runs_with_queue_notification_error),
        managed_backlog=int(row.managed_backlog),
        governance_delayed=int(row.governance_delayed),
        governance_exhausted=int(row.governance_exhausted),
        active_provider_attempts=active_provider_attempts,
        overdrawn_governance_scopes=overdrawn_governance_scopes,
        total_attempts=int(row.total_attempts),
        total_failed_attempts=int(row.total_failed_attempts),
        total_dispatches=int(row.total_dispatches),
        expired_lease_oldest_age_seconds=expired_age,
        worker=worker,
        worker_activity_oldest=worker_activity_oldest,
        timestamp=now,
    )


def collect_task_history(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    audit_event_limit: int | None,
    latency_sample_limit: int = METRICS_LATENCY_SAMPLE_LIMIT,
    latency_window_start: datetime | None = None,
) -> TaskHistorySnapshot:
    """Validate and aggregate one half-open UTC window from retained facts."""

    if audit_event_limit is not None and audit_event_limit < 1:
        raise ValueError("audit_event_limit_must_be_positive")
    if latency_sample_limit < 1:
        raise ValueError("latency_sample_limit_must_be_positive")
    effective_latency_start = latency_window_start or window_start
    event_query = (
        select(AuditEvent)
        .where(
            AuditEvent.occurred_at >= window_start,
            AuditEvent.occurred_at < window_end,
        )
        .order_by(AuditEvent.occurred_at, AuditEvent.id)
    )
    if audit_event_limit is not None:
        event_query = event_query.limit(audit_event_limit + 1)
    try:
        events = list(session.scalars(event_query))
    except (LookupError, OverflowError, TypeError, ValueError):
        # SQLite can contain invalid typed values after an out-of-band write.
        # ORM JSON/enum/timestamp deserialization happens before the retained-row
        # validator sees the event, so preserve the same integrity classification
        # at this narrowly scoped AuditEvent hydration boundary.
        raise AuditIntegrityError("retained audit event failed integrity validation") from None
    if audit_event_limit is not None and len(events) > audit_event_limit:
        raise MetricsObservationLimitExceeded("metrics_audit_event_limit_exceeded")

    event_values = {event_type: 0 for event_type in TASK_EVENT_TYPES}
    for event in events:
        facts = validate_audit_event_for_read(event)
        if facts.event_type in event_values:
            event_values[facts.event_type] += 1

    return TaskHistorySnapshot(
        window_start=window_start,
        window_end=window_end,
        event_counts=event_values,
        audit_events_scanned=len(events),
        queue_latency=_run_latency_summary(
            session,
            observed_at=EvaluationRun.started_at,
            started_at=EvaluationRun.created_at,
            ended_at=EvaluationRun.started_at,
            window_start=effective_latency_start,
            window_end=window_end,
            sample_limit=latency_sample_limit,
        ),
        execution_latency=_run_latency_summary(
            session,
            observed_at=EvaluationRun.finished_at,
            started_at=EvaluationRun.started_at,
            ended_at=EvaluationRun.finished_at,
            window_start=effective_latency_start,
            window_end=window_end,
            sample_limit=latency_sample_limit,
        ),
        end_to_end_latency=_run_latency_summary(
            session,
            observed_at=EvaluationRun.finished_at,
            started_at=EvaluationRun.created_at,
            ended_at=EvaluationRun.finished_at,
            window_start=effective_latency_start,
            window_end=window_end,
            sample_limit=latency_sample_limit,
        ),
        latency_sample_limit=latency_sample_limit,
    )


def collect_operational_snapshot(
    session: Session,
    *,
    worker_stale_seconds: float,
    worker_expected_processes: int,
) -> OperationalSnapshot:
    """Collect the fixed exporter snapshot using one transaction and DB clock."""

    with session.begin():
        configure_read_snapshot(session)
        now = database_clock(session)
        current = collect_task_current(
            session,
            now=now,
            worker_stale_seconds=worker_stale_seconds,
            worker_expected_processes=worker_expected_processes,
        )
        history = collect_task_history(
            session,
            window_start=now - timedelta(seconds=METRICS_AUDIT_WINDOW_SECONDS),
            window_end=now,
            audit_event_limit=METRICS_AUDIT_EVENT_LIMIT,
            latency_sample_limit=METRICS_LATENCY_SAMPLE_LIMIT,
            latency_window_start=now - timedelta(seconds=METRICS_LATENCY_WINDOW_SECONDS),
        )
    return OperationalSnapshot(current=current, history=history)
