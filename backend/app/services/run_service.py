"""Shared creation of immutable evaluation Run snapshots."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.constants import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_POOL_TIMEOUT_SECONDS,
    DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
    DEFAULT_RETRY_BACKOFF_CAP_SECONDS,
    DEFAULT_WRITE_TIMEOUT_SECONDS,
    PROTOCOL_VERSION,
    RETRYABLE_PROVIDER_STATUS_CODES,
)
from app.models import Benchmark, EvaluationRun, Model, RunStatus
from app.schemas.evaluation_run import EvaluationRunCreate
from app.schemas.model import model_run_snapshot_values

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def git_commit_sha() -> str | None:
    """Return the current repository commit without failing source archives."""

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


def build_evaluation_run(
    model: Model,
    benchmark: Benchmark,
    payload: EvaluationRunCreate,
    settings: Settings,
) -> EvaluationRun:
    """Build one pending Run with all mutable dependencies frozen into JSON."""

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
        "model": model_run_snapshot_values(model),
        "benchmark": {
            "id": benchmark.id,
            "slug": benchmark.slug,
            "name": benchmark.name,
            "version": benchmark.version,
            "dataset_hash": benchmark.dataset_hash,
            "question_count": benchmark.question_count,
            "is_demo": benchmark.is_demo,
            "schema_version": benchmark.schema_version,
            "source": benchmark.source,
            "license": benchmark.license,
            "dimension": benchmark.dimension,
            "language": benchmark.language,
        },
        "evaluator": dict(benchmark.evaluator_config),
        "execution": {
            "concurrency": payload.concurrency,
            "timeouts_seconds": {
                "connect": DEFAULT_CONNECT_TIMEOUT_SECONDS,
                "read": payload.read_timeout_seconds,
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
    return EvaluationRun(
        model_id=model.id,
        benchmark_id=benchmark.id,
        status=RunStatus.PENDING,
        protocol_version=PROTOCOL_VERSION,
        model_parameters_snapshot=snapshot,
        benchmark_hash_snapshot=benchmark.dataset_hash,
        prompt_template_snapshot=prompt_snapshot,
        code_commit_sha=git_commit_sha(),
        total_questions=benchmark.question_count,
        max_attempts=settings.worker_max_attempts,
    )
