"""Evaluation Run creation, polling, cancellation, and evidence endpoints."""

from __future__ import annotations

import logging
import math
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PaginationDep, SessionDep, SettingsDep
from app.core.config import Settings
from app.db.model_lock import lock_model_for_update
from app.governance import (
    AuditIntegrityError,
    GovernanceBacklogFull,
    GovernanceIntegrityError,
    GovernanceRepository,
    record_governance_integrity_event,
    validate_audit_identity_for_read,
    validate_audit_payload_for_read,
)
from app.models import (
    AuditEvent,
    AuditRetentionClass,
    Benchmark,
    EvaluationResponse,
    EvaluationRun,
    Question,
    RunStatus,
)
from app.runners.run_leases import CancelDisposition, RunLeaseRepository
from app.schemas.audit import AuditEventList
from app.schemas.evaluation_response import EvaluationResponseList
from app.schemas.evaluation_run import EvaluationRunCreate, EvaluationRunList, EvaluationRunRead
from app.services.run_service import build_evaluation_run
from app.task_queue import QueueUnavailable

router = APIRouter(prefix="/runs", tags=["runs"])
logger = logging.getLogger(__name__)


def _session_factory(session: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=session.get_bind(),
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


def _get_run_or_404(session: SessionDep, run_id: str) -> EvaluationRun:
    run = session.get(EvaluationRun, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "run_not_found", "message": "Evaluation run was not found"},
        )
    return run


def _lease_repository(session: Session, settings: Settings) -> RunLeaseRepository:
    return RunLeaseRepository(
        _session_factory(session),
        lease_for=timedelta(seconds=settings.worker_lease_seconds),
        retry_backoff_base=timedelta(seconds=settings.worker_retry_backoff_base_seconds),
        retry_backoff_cap=timedelta(seconds=settings.worker_retry_backoff_cap_seconds),
    )


def _record_governance_integrity(
    session: Session,
    *,
    run_id: str | None = None,
    model_id: str | None = None,
) -> None:
    session.rollback()
    try:
        record_governance_integrity_event(
            _session_factory(session),
            run_id=run_id,
            model_id=model_id,
        )
    except Exception:
        logger.error(
            "Governance integrity evidence could not be recorded",
            extra={
                "event": "governance_integrity_audit_failed",
                "run_id": run_id,
                "model_id": model_id,
                "result": "not_recorded",
            },
        )


def _audit_event_read_item(event: AuditEvent) -> dict[str, object]:
    minimum_retention = timedelta(
        days=365 if event.retention_class == AuditRetentionClass.SECURITY else 90
    )
    if (
        (
            event.attempt is not None
            and (
                isinstance(event.attempt, bool)
                or not isinstance(event.attempt, int)
                or not 0 <= event.attempt <= 2**31 - 1
            )
        )
        or (
            event.provider_attempt is not None
            and (
                isinstance(event.provider_attempt, bool)
                or not isinstance(event.provider_attempt, int)
                or not 1 <= event.provider_attempt <= 2**31 - 1
            )
        )
        or (
            event.lease_token is not None
            and (
                isinstance(event.lease_token, bool)
                or not isinstance(event.lease_token, int)
                or not 0 <= event.lease_token <= 2**63 - 1
            )
        )
        or (
            event.duration_ms is not None
            and (
                isinstance(event.duration_ms, bool)
                or not isinstance(event.duration_ms, (int, float))
                or not math.isfinite(event.duration_ms)
                or event.duration_ms < 0
            )
        )
        or event.expires_at - event.occurred_at < minimum_retention
    ):
        raise AuditIntegrityError("retained audit event failed integrity validation")
    return {
        "id": validate_audit_identity_for_read("id", event.id, maximum=36),
        "event_type": event.event_type,
        "payload": validate_audit_payload_for_read(
            event.event_type,
            event.payload,
            event.payload_hash,
        ),
        "retention_class": event.retention_class,
        "occurred_at": event.occurred_at,
        "expires_at": event.expires_at,
        "correlation_id": validate_audit_identity_for_read(
            "correlation_id", event.correlation_id, maximum=128
        ),
        "run_id": validate_audit_identity_for_read("run_id", event.run_id, maximum=36),
        "model_id": validate_audit_identity_for_read("model_id", event.model_id, maximum=36),
        "question_id": validate_audit_identity_for_read(
            "question_id", event.question_id, maximum=36
        ),
        "worker_id": validate_audit_identity_for_read("worker_id", event.worker_id, maximum=128),
        "reservation_id": validate_audit_identity_for_read(
            "reservation_id", event.reservation_id, maximum=36
        ),
        "attempt": event.attempt,
        "provider_attempt": event.provider_attempt,
        "lease_token": event.lease_token,
        "duration_ms": event.duration_ms,
    }


@router.get("", response_model=EvaluationRunList, summary="分页列出 Run")
def list_runs(
    session: SessionDep,
    pagination: PaginationDep,
    model_id: str | None = None,
    benchmark_id: str | None = None,
    run_status: RunStatus | None = None,
    protocol_version: str | None = None,
) -> EvaluationRunList:
    filters = []
    if model_id:
        filters.append(EvaluationRun.model_id == model_id)
    if benchmark_id:
        filters.append(EvaluationRun.benchmark_id == benchmark_id)
    if run_status:
        filters.append(EvaluationRun.status == run_status)
    if protocol_version:
        filters.append(EvaluationRun.protocol_version == protocol_version)
    total = session.scalar(select(func.count()).select_from(EvaluationRun).where(*filters)) or 0
    items = list(
        session.scalars(
            select(EvaluationRun)
            .where(*filters)
            .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id)
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
    )
    return EvaluationRunList(
        items=items, total=total, offset=pagination.offset, limit=pagination.limit
    )


@router.post(
    "",
    response_model=EvaluationRunRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建并后台启动 Run",
)
async def create_run(
    payload: EvaluationRunCreate,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> EvaluationRun:
    # Serialize Run creation with endpoint/credential mutation on every
    # supported database so a pending Run cannot race its Provider guard.
    model = lock_model_for_update(session, payload.model_id)
    if model is None:
        session.rollback()
        raise HTTPException(
            status_code=404, detail={"code": "model_not_found", "message": "Model was not found"}
        )
    if not model.enabled:
        session.rollback()
        raise HTTPException(
            status_code=409, detail={"code": "model_disabled", "message": "Model is disabled"}
        )
    benchmark = session.get(Benchmark, payload.benchmark_id)
    if benchmark is None:
        session.rollback()
        raise HTTPException(
            status_code=404,
            detail={"code": "benchmark_not_found", "message": "Benchmark was not found"},
        )

    run = build_evaluation_run(model, benchmark, payload, settings)
    repository = GovernanceRepository(
        sessionmaker(
            bind=session.get_bind(),
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        )
    )
    try:
        repository.admit_run(
            session,
            run,
            provider_type=model.provider_type.value,
            base_url=model.base_url,
        )
    except GovernanceBacklogFull as exc:
        # This async endpoint uses a synchronous SQLAlchemy session. Release
        # the Model/global-scope row locks before handing control back to the
        # event loop, otherwise a concurrent rejected request can block the
        # loop while FastAPI is still finalizing this request dependency.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": exc.code,
                "message": "The managed Run backlog is at its configured limit.",
                "limit": exc.limit,
            },
        ) from None
    except GovernanceIntegrityError:
        _record_governance_integrity(
            session,
            run_id=run.id,
            model_id=model.id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "governance_integrity_error",
                "message": "Governance state failed integrity validation.",
            },
        ) from None
    session.commit()
    correlation_id = run.id
    logger.info(
        "Run persisted before queue notification",
        extra={
            "event": "run_created",
            "correlation_id": correlation_id,
            "run_id": run.id,
            "result": "pending",
        },
    )
    run_queue = request.app.state.run_queue
    if run_queue is not None:
        repository = _lease_repository(session, settings)
        try:
            message_id = await run_queue.publish(run.id, correlation_id=correlation_id)
        except QueueUnavailable:
            logger.warning(
                "Run queue notification unavailable",
                extra={
                    "event": "run_queue_publish_failed",
                    "correlation_id": correlation_id,
                    "run_id": run.id,
                    "result": "observed_unavailable",
                },
            )
            try:
                repository.record_notification_result(run.id, published=False)
            except Exception:
                logger.error(
                    "Run queue failure evidence could not be recorded",
                    extra={
                        "event": "run_queue_audit_failed",
                        "correlation_id": correlation_id,
                        "run_id": run.id,
                    },
                )
        else:
            logger.info(
                "Run queue notification published",
                extra={
                    "event": "run_queue_published",
                    "correlation_id": correlation_id,
                    "run_id": run.id,
                    "message_id": message_id,
                    "result": "published",
                },
            )
            try:
                repository.record_notification_result(run.id, published=True)
            except Exception:
                logger.error(
                    "Run queue success evidence could not be recorded",
                    extra={
                        "event": "run_queue_audit_failed",
                        "correlation_id": correlation_id,
                        "run_id": run.id,
                    },
                )
        session.expire_all()
        refreshed = session.get(EvaluationRun, run.id)
        if refreshed is not None:
            run = refreshed
    return run


@router.get("/{run_id}", response_model=EvaluationRunRead, summary="查看 Run")
def get_run(run_id: str, session: SessionDep) -> EvaluationRun:
    return _get_run_or_404(session, run_id)


@router.get(
    "/{run_id}/audit",
    response_model=AuditEventList,
    summary="分页查看 Run 审计事件",
)
def list_run_audit_events(
    run_id: str,
    session: SessionDep,
    pagination: PaginationDep,
) -> AuditEventList:
    """Return retained, typed events in stable chronological order."""

    _get_run_or_404(session, run_id)
    filters = (AuditEvent.run_id == run_id,)
    total = session.scalar(select(func.count()).select_from(AuditEvent).where(*filters)) or 0
    events = list(
        session.scalars(
            select(AuditEvent)
            .where(*filters)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
    )
    try:
        items = [_audit_event_read_item(event) for event in events]
    except AuditIntegrityError as exc:
        logger.error(
            "Retained Run audit event failed read validation",
            extra={
                "event": "run_audit_integrity_error",
                "run_id": run_id,
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
    return AuditEventList(
        items=items,
        total=total,
        offset=pagination.offset,
        limit=pagination.limit,
    )


@router.post("/{run_id}/cancel", response_model=EvaluationRunRead, summary="协作式取消 Run")
def cancel_run(run_id: str, session: SessionDep, settings: SettingsDep) -> EvaluationRun:
    repository = _lease_repository(session, settings)
    disposition = repository.request_cancel(run_id)
    if disposition == CancelDisposition.NOT_FOUND:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "run_not_found", "message": "Evaluation run was not found"},
        )
    session.expire_all()
    run = session.get(EvaluationRun, run_id)
    if run is None:  # Defensive: the row cannot disappear because Runs are not deletable.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "run_not_found", "message": "Evaluation run was not found"},
        )
    return run


@router.get("/{run_id}/responses", response_model=EvaluationResponseList, summary="查看逐题结果")
def list_responses(
    run_id: str,
    session: SessionDep,
    pagination: PaginationDep,
) -> EvaluationResponseList:
    _get_run_or_404(session, run_id)
    total = (
        session.scalar(
            select(func.count())
            .select_from(EvaluationResponse)
            .where(EvaluationResponse.run_id == run_id)
        )
        or 0
    )
    rows = session.execute(
        select(EvaluationResponse, Question)
        .join(Question, Question.id == EvaluationResponse.question_id)
        .where(EvaluationResponse.run_id == run_id)
        .order_by(Question.position)
        .offset(pagination.offset)
        .limit(pagination.limit)
    ).all()
    items = [
        {
            "id": response.id,
            "run_id": response.run_id,
            "question_id": response.question_id,
            "raw_response": response.raw_response,
            "parsed_answer": response.parsed_answer,
            "reference_answer_snapshot": response.reference_answer_snapshot,
            "score": response.score,
            "evaluator_name": response.evaluator_name,
            "latency_ms": response.latency_ms,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "estimated_cost": float(response.estimated_cost)
            if response.estimated_cost is not None
            else None,
            "provider_request_id": response.provider_request_id,
            "returned_model": response.returned_model,
            "system_fingerprint": response.system_fingerprint,
            "finish_reason": response.finish_reason,
            "http_attempt_count": response.http_attempt_count,
            "error_type": response.error_type,
            "error_message": response.error_message,
            "created_at": response.created_at,
            "question_external_id": question.external_id,
            "question_type": question.question_type.value,
            "prompt": question.prompt,
            "choices": question.choices,
        }
        for response, question in rows
    ]
    return EvaluationResponseList(
        items=items, total=total, offset=pagination.offset, limit=pagination.limit
    )
