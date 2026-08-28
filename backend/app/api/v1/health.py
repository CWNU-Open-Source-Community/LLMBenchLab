"""Liveness, readiness, task operations, and capability endpoints."""

import asyncio
import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import case, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import SessionDep, SettingsDep
from app.core.config import get_settings
from app.core.constants import PROTOCOL_VERSION
from app.core.time import utc_now
from app.db.prepare_migrations import database_heads, expected_database_heads
from app.db.session import SessionLocal
from app.governance import (
    AuditIntegrityError,
    validate_audit_identity_for_read,
    validate_audit_payload_for_read,
)
from app.models import (
    AuditEvent,
    AuditRetentionClass,
    EvaluationRun,
    GovernanceRunStatus,
    GovernanceScope,
    ProviderCallReservation,
    ProviderCallReservationState,
    RunStatus,
)
from app.schemas.system import (
    HealthResponse,
    InfoResponse,
    LivenessResponse,
    ReadinessResponse,
    TaskEventCounts,
    TaskHistoryResponse,
    TaskLatencyPercentiles,
    TaskMetricsResponse,
)
from app.task_queue import QueueUnavailable

router = APIRouter(tags=["system"])
logger = logging.getLogger(__name__)

_HISTORY_MAX_WINDOW_HOURS = 90 * 24
_HISTORY_LATENCY_SAMPLE_LIMIT = 10_000
_TASK_EVENT_TYPES = (
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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _database_clock(session: Session) -> datetime:
    value = session.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("Database did not return a timestamp for task metrics")
    return _as_utc(value)


def _percentile(values: Sequence[float], percentage: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one sample")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentage / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _latency_summary(values: Sequence[float]) -> TaskLatencyPercentiles:
    truncated = len(values) > _HISTORY_LATENCY_SAMPLE_LIMIT
    selected = list(values[:_HISTORY_LATENCY_SAMPLE_LIMIT])
    if not selected:
        return TaskLatencyPercentiles(
            sample_count=0,
            truncated=False,
            p50_ms=None,
            p95_ms=None,
            p99_ms=None,
        )
    return TaskLatencyPercentiles(
        sample_count=len(selected),
        truncated=truncated,
        p50_ms=round(_percentile(selected, 50), 6),
        p95_ms=round(_percentile(selected, 95), 6),
        p99_ms=round(_percentile(selected, 99), 6),
    )


def _run_latency_summary(
    session: Session,
    *,
    observed_at: Any,
    started_at: Any,
    ended_at: Any,
    window_start: datetime,
    window_end: datetime,
) -> TaskLatencyPercentiles:
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
        .limit(_HISTORY_LATENCY_SAMPLE_LIMIT + 1)
    ).all()
    values = [
        (_as_utc(end_value) - _as_utc(start_value)).total_seconds() * 1000
        for start_value, end_value in rows
    ]
    return _latency_summary(values)


def _configure_task_history_snapshot(session: Session) -> None:
    """Start the strongest portable read snapshot before any history query."""

    dialect = session.get_bind().dialect.name
    connection = session.connection()
    if dialect == "sqlite":
        # SQLite's legacy driver mode does not begin a transaction for SELECT.
        connection.exec_driver_sql("BEGIN")
    elif dialect == "postgresql":
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")


def _validate_task_history_event(event: AuditEvent) -> str:
    """Validate every retained fact before it contributes to a counter."""

    try:
        event_type = event.event_type
        validate_audit_payload_for_read(event_type, event.payload, event.payload_hash)
        validate_audit_identity_for_read("id", event.id, maximum=36)
        validate_audit_identity_for_read("event_key", event.event_key, maximum=255)
        validate_audit_identity_for_read(
            "correlation_id",
            event.correlation_id,
            maximum=128,
        )
        validate_audit_identity_for_read("run_id", event.run_id, maximum=36)
        validate_audit_identity_for_read("model_id", event.model_id, maximum=36)
        validate_audit_identity_for_read("question_id", event.question_id, maximum=36)
        validate_audit_identity_for_read("worker_id", event.worker_id, maximum=128)
        validate_audit_identity_for_read(
            "reservation_id",
            event.reservation_id,
            maximum=36,
        )

        if not isinstance(event.retention_class, AuditRetentionClass):
            raise ValueError("invalid audit retention class")
        if not isinstance(event.occurred_at, datetime) or not isinstance(
            event.expires_at,
            datetime,
        ):
            raise ValueError("invalid audit retention timestamps")
        minimum_retention = timedelta(
            days=365 if event.retention_class == AuditRetentionClass.SECURITY else 90
        )
        if _as_utc(event.expires_at) - _as_utc(event.occurred_at) < minimum_retention:
            raise ValueError("invalid audit retention interval")

        if event.attempt is not None and (
            isinstance(event.attempt, bool)
            or not isinstance(event.attempt, int)
            or not 0 <= event.attempt <= 2**31 - 1
        ):
            raise ValueError("invalid audit attempt")
        if event.provider_attempt is not None and (
            isinstance(event.provider_attempt, bool)
            or not isinstance(event.provider_attempt, int)
            or not 1 <= event.provider_attempt <= 2**31 - 1
        ):
            raise ValueError("invalid audit provider attempt")
        if event.lease_token is not None and (
            isinstance(event.lease_token, bool)
            or not isinstance(event.lease_token, int)
            or not 0 <= event.lease_token <= 2**63 - 1
        ):
            raise ValueError("invalid audit lease token")
        if event.duration_ms is not None and (
            isinstance(event.duration_ms, bool)
            or not isinstance(event.duration_ms, (int, float))
            or not math.isfinite(event.duration_ms)
            or event.duration_ms < 0
        ):
            raise ValueError("invalid audit duration")
    except AuditIntegrityError:
        raise
    except (OverflowError, TypeError, ValueError):
        raise AuditIntegrityError("retained audit event failed integrity validation") from None
    return event_type


def _database_readiness() -> tuple[str, str, list[str]]:
    database_status = "ok"
    schema_status = "ok"
    errors: list[str] = []
    try:
        with SessionLocal() as readiness_session:
            readiness_session.execute(text("SELECT 1"))
            current_heads = set(database_heads(readiness_session.connection()))
            expected_heads = set(expected_database_heads())
            if current_heads != expected_heads:
                schema_status = "not_ready"
                errors.append("schema_not_ready")
    except SQLAlchemyError:
        database_status = "unavailable"
        schema_status = "unavailable"
        errors.append("database_unavailable")
    except Exception:
        schema_status = "unavailable"
        errors.append("schema_check_unavailable")
    return database_status, schema_status, errors


@router.get("/live", response_model=LivenessResponse, summary="检查 API 进程存活状态")
async def liveness() -> LivenessResponse:
    """Return process liveness without contacting database, Redis, or a Provider."""

    settings = get_settings()
    return LivenessResponse(status="live", version=settings.app_version, timestamp=utc_now())


@router.get("/health", response_model=HealthResponse, summary="检查 API 与数据库健康状态")
def health(session: SessionDep, settings: SettingsDep) -> HealthResponse:
    """Check only local persistence; no model provider is contacted."""

    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "database_unavailable", "message": "Database health check failed"},
        ) from exc
    return HealthResponse(
        status="ok",
        database="ok",
        version=settings.app_version,
        timestamp=utc_now(),
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
    summary="检查数据库版本与 Redis 组件就绪状态",
)
async def readiness(
    request: Request,
    settings: SettingsDep,
) -> Any:
    """Return componentized readiness without exposing connection details."""

    database_task = asyncio.create_task(asyncio.to_thread(_database_readiness))
    run_queue = request.app.state.run_queue
    queue_task = (
        asyncio.create_task(
            asyncio.wait_for(
                run_queue.ping(),
                timeout=settings.redis_operation_timeout_seconds,
            )
        )
        if run_queue is not None
        else None
    )

    try:
        database_status, schema_status, errors = await asyncio.wait_for(
            database_task,
            timeout=settings.readiness_database_timeout_seconds,
        )
    except TimeoutError:
        database_status = "unavailable"
        schema_status = "unavailable"
        errors = ["database_unavailable"]

    queue_status = "disabled"
    if queue_task is not None:
        try:
            await queue_task
        except (QueueUnavailable, TimeoutError):
            queue_status = "unavailable"
            errors.append("queue_unavailable")
        except Exception:
            queue_status = "unavailable"
            errors.append("queue_check_unavailable")
        else:
            queue_status = "ok"

    database_ready = database_status == "ok" and schema_status == "ok"
    is_ready = database_ready and queue_status != "unavailable"
    overall_status = "ready" if is_ready else "degraded" if database_ready else "not_ready"
    payload = ReadinessResponse(
        status=overall_status,
        database=database_status,
        schema=schema_status,
        queue=queue_status,
        accepting_runs=database_ready,
        database_reconciliation="available" if database_ready else "unavailable",
        errors=errors,
        version=settings.app_version,
        timestamp=utc_now(),
    )
    if is_ready:
        return payload
    return JSONResponse(status_code=503, content=payload.model_dump(mode="json", by_alias=True))


@router.get(
    "/tasks/metrics",
    response_model=TaskMetricsResponse,
    summary="查看数据库派生的任务运维指标",
)
def task_metrics(session: SessionDep) -> TaskMetricsResponse:
    """Expose database-derived gauges; metrics never override task truth."""

    now = _database_clock(session)
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
                            & EvaluationRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
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
        )
    ).one()
    active_provider_attempts = (
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
    overdrawn_governance_scopes = (
        session.scalar(
            select(func.count(GovernanceScope.id)).where(GovernanceScope.overdrawn.is_(True))
        )
        or 0
    )
    return TaskMetricsResponse(
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
        active_provider_attempts=int(active_provider_attempts),
        overdrawn_governance_scopes=int(overdrawn_governance_scopes),
        total_attempts=int(row.total_attempts),
        total_failed_attempts=int(row.total_failed_attempts),
        total_dispatches=int(row.total_dispatches),
        timestamp=now,
    )


@router.get(
    "/tasks/history",
    response_model=TaskHistoryResponse,
    summary="查看有界 UTC 窗口内的任务历史指标",
)
def task_history(
    session: SessionDep,
    window_hours: Annotated[
        int,
        Query(ge=1, le=_HISTORY_MAX_WINDOW_HOURS),
    ] = 24,
) -> TaskHistoryResponse:
    """Aggregate retained audit counters and persisted Run latency facts."""

    try:
        with session.begin():
            _configure_task_history_snapshot(session)
            window_end = _database_clock(session)
            window_start = window_end - timedelta(hours=window_hours)
            events = list(
                session.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.occurred_at >= window_start,
                        AuditEvent.occurred_at < window_end,
                    )
                    .order_by(AuditEvent.occurred_at, AuditEvent.id)
                )
            )
            event_values = {event_type: 0 for event_type in _TASK_EVENT_TYPES}
            for event in events:
                event_type = _validate_task_history_event(event)
                if event_type in event_values:
                    event_values[event_type] += 1
            event_counts = TaskEventCounts(
                total=sum(event_values.values()),
                **event_values,
            )

            queue_latency = _run_latency_summary(
                session,
                observed_at=EvaluationRun.started_at,
                started_at=EvaluationRun.created_at,
                ended_at=EvaluationRun.started_at,
                window_start=window_start,
                window_end=window_end,
            )
            execution_latency = _run_latency_summary(
                session,
                observed_at=EvaluationRun.finished_at,
                started_at=EvaluationRun.started_at,
                ended_at=EvaluationRun.finished_at,
                window_start=window_start,
                window_end=window_end,
            )
            end_to_end_latency = _run_latency_summary(
                session,
                observed_at=EvaluationRun.finished_at,
                started_at=EvaluationRun.created_at,
                ended_at=EvaluationRun.finished_at,
                window_start=window_start,
                window_end=window_end,
            )
    except (AuditIntegrityError, LookupError) as exc:
        logger.error(
            "Retained task history audit event failed read validation",
            extra={
                "event": "task_history_audit_integrity_error",
                "result": "rejected",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "audit_event_integrity_error",
                "message": "A retained audit event failed integrity validation",
            },
        ) from exc
    return TaskHistoryResponse(
        window_start=window_start,
        window_end=window_end,
        window_hours=window_hours,
        event_counts=event_counts,
        queue_latency=queue_latency,
        execution_latency=execution_latency,
        end_to_end_latency=end_to_end_latency,
        latency_sample_limit=_HISTORY_LATENCY_SAMPLE_LIMIT,
        timestamp=window_end,
    )


@router.get("/info", response_model=InfoResponse, summary="查看服务与协议信息")
def info(settings: SettingsDep) -> InfoResponse:
    return InfoResponse(
        name=settings.app_name,
        version=settings.app_version,
        api_version="v1",
        protocol_version=PROTOCOL_VERSION,
        environment=settings.environment,
        capabilities={
            "providers": ["mock", "openai_compatible"],
            "question_types": ["exact_match", "multiple_choice", "numeric"],
            "runner": "independent_database_lease_worker",
        },
    )
