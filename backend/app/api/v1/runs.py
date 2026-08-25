"""Evaluation Run creation, polling, cancellation, and evidence endpoints."""

from __future__ import annotations

import logging
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PaginationDep, SessionDep, SettingsDep
from app.core.config import Settings
from app.core.constants import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_POOL_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
    DEFAULT_RETRY_BACKOFF_CAP_SECONDS,
    DEFAULT_WRITE_TIMEOUT_SECONDS,
    PROTOCOL_VERSION,
    RETRYABLE_PROVIDER_STATUS_CODES,
)
from app.models import Benchmark, EvaluationResponse, EvaluationRun, Model, Question, RunStatus
from app.runners.run_leases import CancelDisposition, RunLeaseRepository
from app.schemas.evaluation_response import EvaluationResponseList
from app.schemas.evaluation_run import EvaluationRunCreate, EvaluationRunList, EvaluationRunRead
from app.task_queue import QueueUnavailable

router = APIRouter(prefix="/runs", tags=["runs"])
PROJECT_ROOT = Path(__file__).resolve().parents[4]
logger = logging.getLogger(__name__)


def _git_commit_sha() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and len(value) == 40 else None


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
        sessionmaker(
            bind=session.get_bind(),
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        ),
        lease_for=timedelta(seconds=settings.worker_lease_seconds),
        retry_backoff_base=timedelta(seconds=settings.worker_retry_backoff_base_seconds),
        retry_backoff_cap=timedelta(seconds=settings.worker_retry_backoff_cap_seconds),
    )


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
    model = session.get(Model, payload.model_id)
    if model is None:
        raise HTTPException(
            status_code=404, detail={"code": "model_not_found", "message": "Model was not found"}
        )
    if not model.enabled:
        raise HTTPException(
            status_code=409, detail={"code": "model_disabled", "message": "Model is disabled"}
        )
    benchmark = session.get(Benchmark, payload.benchmark_id)
    if benchmark is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "benchmark_not_found", "message": "Benchmark was not found"},
        )

    prompt_snapshot = dict(benchmark.prompt_template)
    if payload.system_prompt is not None:
        prompt_snapshot["system"] = payload.system_prompt
    model_defaults = dict(model.default_parameters or {})
    requested_fields = payload.model_fields_set
    generation = {
        field: (
            getattr(payload, field)
            if field in requested_fields or field not in model_defaults
            else model_defaults[field]
        )
        for field in ("temperature", "top_p", "max_tokens", "seed")
    }
    snapshot: dict[str, Any] = {
        "generation": generation,
        "model": {
            "id": model.id,
            "name": model.name,
            "remote_model_name": model.remote_model_name,
            "adapter_type": model.provider_type.value,
            "base_url": model.base_url,
            "api_key_env": model.api_key_env,
            "input_price_per_million": (
                str(model.input_price_per_million)
                if model.input_price_per_million is not None
                else None
            ),
            "output_price_per_million": (
                str(model.output_price_per_million)
                if model.output_price_per_million is not None
                else None
            ),
            "currency_assumption": "USD",
            "default_parameters": dict(model.default_parameters or {}),
        },
        "benchmark": {
            "id": benchmark.id,
            "slug": benchmark.slug,
            "name": benchmark.name,
            "version": benchmark.version,
            "dataset_hash": benchmark.dataset_hash,
            "question_count": benchmark.question_count,
            "is_demo": benchmark.is_demo,
        },
        "evaluator": dict(benchmark.evaluator_config),
        "execution": {
            "concurrency": payload.concurrency,
            "timeouts_seconds": {
                "connect": DEFAULT_CONNECT_TIMEOUT_SECONDS,
                "read": DEFAULT_READ_TIMEOUT_SECONDS,
                "write": DEFAULT_WRITE_TIMEOUT_SECONDS,
                "pool": DEFAULT_POOL_TIMEOUT_SECONDS,
            },
            "retry_policy": {
                "name": "bounded_exponential_backoff",
                "max_retries": DEFAULT_MAX_RETRIES,
                "max_attempts": DEFAULT_MAX_RETRIES + 1,
                "backoff_base_seconds": DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
                "backoff_cap_seconds": DEFAULT_RETRY_BACKOFF_CAP_SECONDS,
                "retryable_status_codes": list(RETRYABLE_PROVIDER_STATUS_CODES),
            },
            "task_delivery": "at_least_once",
            "task_max_attempts": settings.worker_max_attempts,
            "restart_recovery": "database_lease_resume_missing_responses",
        },
    }
    run = EvaluationRun(
        model_id=model.id,
        benchmark_id=benchmark.id,
        status=RunStatus.PENDING,
        protocol_version=PROTOCOL_VERSION,
        model_parameters_snapshot=snapshot,
        benchmark_hash_snapshot=benchmark.dataset_hash,
        prompt_template_snapshot=prompt_snapshot,
        code_commit_sha=_git_commit_sha(),
        total_questions=benchmark.question_count,
        max_attempts=settings.worker_max_attempts,
    )
    session.add(run)
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
