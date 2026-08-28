"""Liveness, readiness, task operations, and capability endpoints."""

import asyncio
import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import SessionDep, SettingsDep
from app.core.config import get_settings
from app.core.constants import PROTOCOL_VERSION
from app.core.time import utc_now
from app.db.prepare_migrations import database_heads, expected_database_heads
from app.db.session import SessionLocal
from app.governance import AuditIntegrityError
from app.observability import (
    METRICS_LATENCY_SAMPLE_LIMIT,
    collect_task_current,
    collect_task_history,
    configure_read_snapshot,
    database_clock,
    latency_summary,
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
_HISTORY_LATENCY_SAMPLE_LIMIT = METRICS_LATENCY_SAMPLE_LIMIT


def _latency_summary(values: Sequence[float]) -> TaskLatencyPercentiles:
    """Compatibility wrapper for the public history response schema."""

    snapshot = latency_summary(values, sample_limit=_HISTORY_LATENCY_SAMPLE_LIMIT)
    return TaskLatencyPercentiles(
        sample_count=snapshot.sample_count,
        truncated=snapshot.truncated,
        p50_ms=snapshot.p50_ms,
        p95_ms=snapshot.p95_ms,
        p99_ms=snapshot.p99_ms,
    )


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
def task_metrics(session: SessionDep, settings: SettingsDep) -> TaskMetricsResponse:
    """Expose database-derived gauges; metrics never override task truth."""

    with session.begin():
        configure_read_snapshot(session)
        now = database_clock(session)
        snapshot = collect_task_current(
            session,
            now=now,
            worker_stale_seconds=settings.worker_progress_stale_seconds,
            worker_expected_processes=settings.worker_expected_processes,
        )
    return TaskMetricsResponse(
        pending=snapshot.pending,
        due_pending=snapshot.due_pending,
        running=snapshot.running,
        expired_running=snapshot.expired_running,
        active_cancellation_requests=snapshot.active_cancellation_requests,
        retry_scheduled=snapshot.retry_scheduled,
        dead_lettered=snapshot.dead_lettered,
        runs_with_queue_notification_error=snapshot.runs_with_queue_notification_error,
        managed_backlog=snapshot.managed_backlog,
        governance_delayed=snapshot.governance_delayed,
        governance_exhausted=snapshot.governance_exhausted,
        active_provider_attempts=snapshot.active_provider_attempts,
        overdrawn_governance_scopes=snapshot.overdrawn_governance_scopes,
        total_attempts=snapshot.total_attempts,
        total_failed_attempts=snapshot.total_failed_attempts,
        total_dispatches=snapshot.total_dispatches,
        worker_expected_processes=snapshot.worker.expected,
        worker_registered_processes=snapshot.worker.registered,
        worker_live_processes=snapshot.worker.live,
        worker_stalled_processes=snapshot.worker.stalled,
        worker_shortfall_processes=snapshot.worker.shortfall,
        worker_stale_after_seconds=snapshot.worker.stale_seconds,
        worker_last_seen_at=snapshot.worker.last_seen_at,
        worker_last_scan_at=snapshot.worker.last_scan_at,
        worker_last_claim_at=snapshot.worker.last_claim_at,
        worker_last_progress_at=snapshot.worker.last_progress_at,
        worker_last_lease_heartbeat_at=snapshot.worker.last_lease_heartbeat_at,
        timestamp=snapshot.timestamp,
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
            configure_read_snapshot(session)
            window_end = database_clock(session)
            window_start = window_end - timedelta(hours=window_hours)
            snapshot = collect_task_history(
                session,
                window_start=window_start,
                window_end=window_end,
                audit_event_limit=None,
                latency_sample_limit=_HISTORY_LATENCY_SAMPLE_LIMIT,
            )
            event_counts = TaskEventCounts(
                total=sum(snapshot.event_counts.values()),
                **snapshot.event_counts,
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
        queue_latency=TaskLatencyPercentiles(
            sample_count=snapshot.queue_latency.sample_count,
            truncated=snapshot.queue_latency.truncated,
            p50_ms=snapshot.queue_latency.p50_ms,
            p95_ms=snapshot.queue_latency.p95_ms,
            p99_ms=snapshot.queue_latency.p99_ms,
        ),
        execution_latency=TaskLatencyPercentiles(
            sample_count=snapshot.execution_latency.sample_count,
            truncated=snapshot.execution_latency.truncated,
            p50_ms=snapshot.execution_latency.p50_ms,
            p95_ms=snapshot.execution_latency.p95_ms,
            p99_ms=snapshot.execution_latency.p99_ms,
        ),
        end_to_end_latency=TaskLatencyPercentiles(
            sample_count=snapshot.end_to_end_latency.sample_count,
            truncated=snapshot.end_to_end_latency.truncated,
            p50_ms=snapshot.end_to_end_latency.p50_ms,
            p95_ms=snapshot.end_to_end_latency.p95_ms,
            p99_ms=snapshot.end_to_end_latency.p99_ms,
        ),
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
