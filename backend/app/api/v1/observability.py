"""Controlled Prometheus exposition backed by one bounded database snapshot."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.deps import SettingsDep
from app.core.config import Settings
from app.db.session import SessionLocal
from app.governance import AuditIntegrityError
from app.observability import MetricsObservationLimitExceeded, OperationalSnapshot
from app.observability.prometheus import (
    PROMETHEUS_CONTENT_TYPE,
    PrometheusRenderingError,
    render_prometheus,
)
from app.observability.snapshot import collect_operational_snapshot
from app.task_queue import QueueUnavailable

router = APIRouter(tags=["system"])
logger = logging.getLogger(__name__)

_COLLECTION_LOCK = Lock()


def _consume_collection_result(task: asyncio.Task[tuple[object, object]]) -> None:
    """Retrieve a detached collection result without reflecting its exception."""

    with suppress(BaseException):
        task.result()


def _collect_database_snapshot(settings: Settings) -> OperationalSnapshot:
    """Open the database session inside the worker thread that owns it."""

    with SessionLocal() as session:
        return collect_operational_snapshot(
            session,
            worker_stale_seconds=settings.worker_progress_stale_seconds,
            worker_expected_processes=settings.worker_expected_processes,
        )


async def _observe_queue(request: Request, settings: Settings) -> tuple[bool, bool]:
    run_queue = request.app.state.run_queue
    if run_queue is None:
        return False, False
    try:
        available = await asyncio.wait_for(
            run_queue.ping(),
            timeout=settings.redis_operation_timeout_seconds,
        )
    except (QueueUnavailable, TimeoutError):
        logger.warning(
            "Metrics queue observation unavailable",
            extra={
                "event": "metrics_queue_unavailable",
                "error_code": "queue_unavailable",
                "result": "unavailable",
            },
        )
        return True, False
    except Exception:
        logger.warning(
            "Metrics queue observation failed",
            extra={
                "event": "metrics_queue_check_failed",
                "error_code": "queue_check_unavailable",
                "result": "unavailable",
            },
        )
        return True, False
    return True, bool(available)


async def _collect_with_gate(
    request: Request,
    settings: Settings,
) -> tuple[object, object]:
    """Own the process gate until both observations really reach a terminal state."""

    try:
        database_task = asyncio.create_task(asyncio.to_thread(_collect_database_snapshot, settings))
        queue_task = asyncio.create_task(_observe_queue(request, settings))
        collection = asyncio.gather(
            database_task,
            queue_task,
            return_exceptions=True,
        )
        while True:
            try:
                return await asyncio.shield(collection)
            except asyncio.CancelledError:
                # Request cancellation cannot reach this detached owner.  If a
                # lifecycle cancellation does, do not propagate it into the
                # synchronous database thread or release the gate early.
                if collection.done():
                    return collection.result()
    finally:
        _COLLECTION_LOCK.release()


def _failure(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


@router.get(
    "/metrics/prometheus",
    response_class=Response,
    responses={
        429: {"description": "Another scrape is already collecting"},
        500: {"description": "Audit integrity or rendering failure"},
        503: {"description": "Database unavailable or observation cap exceeded"},
    },
    summary="抓取固定低基数 Prometheus 指标",
)
async def prometheus_metrics(request: Request, settings: SettingsDep) -> Response:
    """Return one non-cached exposition; Redis remains a non-authoritative observation."""

    if request.url.query:
        raise _failure(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "metrics_query_parameters_not_allowed",
            "Metrics scrape does not accept query parameters",
        )
    if not _COLLECTION_LOCK.acquire(blocking=False):
        raise _failure(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "metrics_scrape_in_progress",
            "A metrics scrape is already in progress",
        )

    try:
        collection = asyncio.create_task(
            _collect_with_gate(request, settings),
            name="prometheus-metrics-collection",
        )
    except BaseException:
        _COLLECTION_LOCK.release()
        raise
    collection.add_done_callback(_consume_collection_result)

    database_result, queue_result = await asyncio.shield(collection)

    if isinstance(database_result, (AuditIntegrityError, LookupError)):
        logger.error(
            "Metrics audit event failed read validation",
            extra={
                "event": "metrics_audit_integrity_error",
                "error_code": "audit_event_integrity_error",
                "result": "rejected",
            },
        )
        raise _failure(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "audit_event_integrity_error",
            "A retained audit event failed integrity validation",
        )
    if isinstance(database_result, MetricsObservationLimitExceeded):
        logger.warning(
            "Metrics audit observation limit exceeded",
            extra={
                "event": "metrics_observation_limit_exceeded",
                "error_code": "metrics_observation_limit_exceeded",
                "result": "rejected",
            },
        )
        raise _failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "metrics_observation_limit_exceeded",
            "Metrics observation limit was exceeded",
        )
    if isinstance(database_result, Exception):
        logger.error(
            "Metrics database collection failed",
            extra={
                "event": "metrics_database_unavailable",
                "error_code": "metrics_database_unavailable",
                "result": "unavailable",
            },
        )
        raise _failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "metrics_database_unavailable",
            "Metrics database collection failed",
        )
    if not isinstance(database_result, OperationalSnapshot):
        raise _failure(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "metrics_database_unavailable",
            "Metrics database collection failed",
        )

    if isinstance(queue_result, Exception):
        queue_configured = request.app.state.run_queue is not None
        queue_available = False
        logger.warning(
            "Metrics queue observation failed",
            extra={
                "event": "metrics_queue_check_failed",
                "error_code": "queue_check_unavailable",
                "result": "unavailable",
            },
        )
    else:
        queue_configured, queue_available = queue_result

    try:
        body = render_prometheus(
            database_result,
            queue_configured=queue_configured,
            queue_available=queue_available,
            recovery_alert_seconds=settings.worker_recovery_alert_seconds,
        )
    except (KeyError, PrometheusRenderingError, TypeError, ValueError) as exc:
        logger.error(
            "Metrics rendering failed",
            extra={
                "event": "metrics_rendering_failed",
                "error_code": "metrics_rendering_error",
                "result": "rejected",
            },
        )
        raise _failure(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "metrics_rendering_error",
            "Metrics rendering failed",
        ) from exc

    return Response(
        content=body,
        status_code=status.HTTP_200_OK,
        headers={
            "Content-Type": PROMETHEUS_CONTENT_TYPE,
            "Cache-Control": "no-store",
        },
    )
