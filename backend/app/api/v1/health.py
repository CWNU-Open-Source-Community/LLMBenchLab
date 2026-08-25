"""Liveness, readiness, task operations, and capability endpoints."""

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import case, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import SessionDep, SettingsDep
from app.core.config import get_settings
from app.core.constants import PROTOCOL_VERSION
from app.core.time import utc_now
from app.db.prepare_migrations import database_heads, expected_database_heads
from app.db.session import SessionLocal
from app.models import EvaluationRun, RunStatus
from app.schemas.system import (
    HealthResponse,
    InfoResponse,
    LivenessResponse,
    ReadinessResponse,
    TaskMetricsResponse,
)
from app.task_queue import QueueUnavailable

router = APIRouter(tags=["system"])


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

    now = func.current_timestamp()
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
            func.coalesce(func.sum(EvaluationRun.attempt_count), 0).label("total_attempts"),
        )
    ).one()
    return TaskMetricsResponse(
        pending=int(row.pending),
        due_pending=int(row.due_pending),
        running=int(row.running),
        expired_running=int(row.expired_running),
        active_cancellation_requests=int(row.active_cancellation_requests),
        retry_scheduled=int(row.retry_scheduled),
        dead_lettered=int(row.dead_lettered),
        runs_with_queue_notification_error=int(row.runs_with_queue_notification_error),
        total_attempts=int(row.total_attempts),
        timestamp=utc_now(),
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
