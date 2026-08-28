"""Atomic, secret-safe export of all persisted evidence for one Run."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import utc_now
from app.models import Benchmark, EvaluationResponse, EvaluationRun, Model, Question
from app.models.enums import TERMINAL_RUN_STATUSES
from app.security import normalize_http_attempt_count, normalize_provider_metadata

REPORT_SCHEMA_VERSION = "llmbenchlab-run-report-v1"
SUMMARY_FILENAME = "summary.json"
GROUPS_FILENAME = "groups.csv"
RESPONSES_FILENAME = "responses.jsonl"

# Only one of these dimensions is selected for a report. This makes the groups
# a partition: each planned question contributes to exactly one CSV row.
GROUP_FIELD_WHITELIST = (
    "category",
    "domain",
    "subdomain",
    "subject",
    "task",
    "language",
)

_SENSITIVE_MAPPING_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "apikey",
        "accesskey",
        "accesstoken",
        "refreshtoken",
        "secret",
        "clientsecret",
    }
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)\b(?:proxy[-_ ]?)?authorization\b\s*[:=]\s*"
    r"(?:bearer\s+)?(?:[\"']?)[^\s,;\"'}\]]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:[\"']?)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|secret|cookie)(?:[\"']?)\s*[:=]\s*"
    r"(?:[\"']?)[^\s,;\"'}\]]+"
)
_COMMON_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"AIza[0-9A-Za-z_-]{20,})\b"
)


class ReportExportError(RuntimeError):
    """Base class for deterministic report-export failures."""


class ReportValidationError(ReportExportError):
    """Raised when export arguments are invalid."""


class ReportNotReadyError(ReportExportError):
    """Raised when a mutable Run is exported before reaching a terminal state."""


class ReportIntegrityError(ReportExportError):
    """Raised when persisted Run, Question, and Response facts disagree."""


class ReportDestinationExistsError(ReportExportError):
    """Raised instead of overwriting an existing report path."""


@dataclass(frozen=True, slots=True)
class RunReportExport:
    """Paths and record counts for one successfully published report."""

    directory: Path
    summary_path: Path
    groups_path: Path
    responses_path: Path
    response_count: int
    group_count: int
    group_by: str | None


@dataclass(slots=True)
class _GroupAggregate:
    planned_questions: int = 0
    response_count: int = 0
    score_sum: float = 0.0
    completed_outputs: int = 0
    evaluable_responses: int = 0
    error_questions: int = 0
    latency_sum: float = 0.0
    latency_reports: int = 0
    input_tokens_sum: int = 0
    input_token_reports: int = 0
    output_tokens_sum: int = 0
    output_token_reports: int = 0
    estimated_cost_sum: Decimal = Decimal(0)
    estimated_cost_reports: int = 0

    def merge(self, other: _GroupAggregate) -> None:
        self.planned_questions += other.planned_questions
        self.response_count += other.response_count
        self.score_sum += other.score_sum
        self.completed_outputs += other.completed_outputs
        self.evaluable_responses += other.evaluable_responses
        self.error_questions += other.error_questions
        self.latency_sum += other.latency_sum
        self.latency_reports += other.latency_reports
        self.input_tokens_sum += other.input_tokens_sum
        self.input_token_reports += other.input_token_reports
        self.output_tokens_sum += other.output_tokens_sum
        self.output_token_reports += other.output_token_reports
        self.estimated_cost_sum += other.estimated_cost_sum
        self.estimated_cost_reports += other.estimated_cost_reports

    def as_metrics(self) -> dict[str, int | float | None]:
        correct_questions = round(self.score_sum)
        score = self.score_sum / self.planned_questions * 100 if self.planned_questions else 0.0
        completion_rate = (
            self.completed_outputs / self.planned_questions * 100 if self.planned_questions else 0.0
        )
        answered_accuracy = (
            correct_questions / self.evaluable_responses * 100 if self.evaluable_responses else None
        )
        return {
            "total_questions": self.planned_questions,
            "completed_questions": self.response_count,
            "correct_questions": correct_questions,
            "error_questions": self.error_questions,
            "score": score,
            "completion_rate": completion_rate,
            "answered_accuracy": answered_accuracy,
            "average_latency_ms": (
                self.latency_sum / self.latency_reports if self.latency_reports else None
            ),
            "input_tokens": (
                self.input_tokens_sum
                if self.response_count and self.input_token_reports == self.response_count
                else None
            ),
            "output_tokens": (
                self.output_tokens_sum
                if self.response_count and self.output_token_reports == self.response_count
                else None
            ),
            "estimated_cost": (
                float(self.estimated_cost_sum)
                if self.response_count and self.estimated_cost_reports == self.response_count
                else None
            ),
        }

    def as_row(self, group_field: str | None, group_value: str) -> dict[str, Any]:
        correct_questions = round(self.score_sum)
        score = self.score_sum / self.planned_questions * 100 if self.planned_questions else 0.0
        completion_rate = (
            self.completed_outputs / self.planned_questions * 100 if self.planned_questions else 0.0
        )
        answered_accuracy = (
            correct_questions / self.evaluable_responses * 100 if self.evaluable_responses else None
        )
        return {
            "group_field": group_field or "",
            "group_value": group_value,
            "planned_questions": self.planned_questions,
            "response_count": self.response_count,
            "correct_questions": correct_questions,
            "error_questions": self.error_questions,
            "score": score,
            "completion_rate": completion_rate,
            "answered_accuracy": answered_accuracy,
        }


def export_run_report(
    session: Session,
    run_id: str,
    output_directory: str | os.PathLike[str],
    *,
    page_size: int = 500,
    group_by: str | None = None,
    secret_values: Iterable[str] = (),
) -> RunReportExport:
    """Export one terminal Run without overwriting an existing destination.

    All database reads are paged. Files are first written with restrictive
    permissions into a sibling temporary directory, flushed, and then published
    with one directory rename. The caller's SQLAlchemy transaction is never
    committed or rolled back.
    """

    if not isinstance(run_id, str) or not run_id.strip():
        raise ReportValidationError("run_id must be a non-empty string")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 10_000
    ):
        raise ReportValidationError("page_size must be an integer between 1 and 10000")
    normalized_group_by = _normalize_group_by(group_by)
    secrets = tuple(
        sorted(
            {value for value in secret_values if isinstance(value, str) and value},
            key=len,
            reverse=True,
        )
    )

    run = session.get(EvaluationRun, run_id)
    if run is None:
        raise ReportValidationError(f"Run {run_id!r} was not found")
    if run.status not in TERMINAL_RUN_STATUSES:
        raise ReportNotReadyError(
            f"Run {run_id!r} is {run.status.value}; only terminal Runs can be exported"
        )
    model = session.get(Model, run.model_id)
    benchmark = session.get(Benchmark, run.benchmark_id)
    if model is None or benchmark is None:
        raise ReportIntegrityError("Run dependencies are missing")

    destination = _prepare_destination(output_directory)
    selected_group_by = normalized_group_by or _select_group_field(
        session, run.benchmark_id, page_size
    )
    groups = _load_planned_groups(
        session,
        run.benchmark_id,
        page_size,
        selected_group_by,
        secrets,
    )
    planned_count = sum(group.planned_questions for group in groups.values())
    if planned_count != run.total_questions:
        raise ReportIntegrityError(
            "Run total_questions does not match its immutable Benchmark question count"
        )

    temporary_directory = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name[:40]}.tmp-",
            dir=destination.parent,
        )
    )
    os.chmod(temporary_directory, 0o700)
    published = False
    try:
        responses_path = temporary_directory / RESPONSES_FILENAME
        response_count = _write_responses_jsonl(
            session,
            run,
            responses_path,
            groups,
            selected_group_by,
            page_size,
            secrets,
        )
        evidence_metrics = _evidence_metrics(groups)
        if evidence_metrics["completed_questions"] != response_count:
            raise ReportIntegrityError("Exported Response count does not match report evidence")
        if response_count > run.total_questions:
            raise ReportIntegrityError("Persisted Response count exceeds planned questions")

        groups_path = temporary_directory / GROUPS_FILENAME
        group_count = _write_groups_csv(groups_path, groups, selected_group_by)

        summary = _build_summary(
            run,
            model,
            benchmark,
            selected_group_by,
            evidence_metrics,
            response_count,
            group_count,
            secrets,
        )
        summary_path = temporary_directory / SUMMARY_FILENAME
        _write_json_line(summary_path, summary)

        _fsync_directory(temporary_directory)
        try:
            os.rename(temporary_directory, destination)
        except (FileExistsError, IsADirectoryError, NotADirectoryError, OSError) as exc:
            if os.path.lexists(destination):
                raise ReportDestinationExistsError(
                    f"Report destination already exists: {destination}"
                ) from exc
            raise
        published = True
        _fsync_directory(destination.parent)
    finally:
        if not published and temporary_directory.exists():
            shutil.rmtree(temporary_directory)

    return RunReportExport(
        directory=destination,
        summary_path=destination / SUMMARY_FILENAME,
        groups_path=destination / GROUPS_FILENAME,
        responses_path=destination / RESPONSES_FILENAME,
        response_count=response_count,
        group_count=group_count,
        group_by=selected_group_by,
    )


def _normalize_group_by(group_by: str | None) -> str | None:
    if group_by is None:
        return None
    if not isinstance(group_by, str):
        raise ReportValidationError("group_by must be a string or null")
    normalized = group_by.strip().lower()
    if normalized not in GROUP_FIELD_WHITELIST:
        allowed = ", ".join(GROUP_FIELD_WHITELIST)
        raise ReportValidationError(f"group_by must be one of: {allowed}")
    return normalized


def _prepare_destination(output_directory: str | os.PathLike[str]) -> Path:
    try:
        requested = Path(output_directory).expanduser()
    except TypeError as exc:
        raise ReportValidationError("output_directory must be path-like") from exc
    if requested.name in {"", ".", ".."}:
        raise ReportValidationError("output_directory must name a new report directory")
    requested.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = requested.parent.resolve(strict=True)
    destination = parent / requested.name
    if os.path.lexists(destination):
        raise ReportDestinationExistsError(f"Report destination already exists: {destination}")
    return destination


def _select_group_field(session: Session, benchmark_id: str, page_size: int) -> str | None:
    counts = dict.fromkeys(GROUP_FIELD_WHITELIST, 0)
    offset = 0
    while True:
        rows = session.scalars(
            select(Question.metadata_)
            .where(Question.benchmark_id == benchmark_id)
            .order_by(Question.position, Question.id)
            .offset(offset)
            .limit(page_size)
        ).all()
        if not rows:
            break
        for metadata in rows:
            values = metadata if isinstance(metadata, Mapping) else {}
            for field in GROUP_FIELD_WHITELIST:
                if _metadata_group_value(values.get(field)) is not None:
                    counts[field] += 1
        offset += len(rows)
    best_count = max(counts.values(), default=0)
    if best_count == 0:
        return None
    return next(field for field in GROUP_FIELD_WHITELIST if counts[field] == best_count)


def _load_planned_groups(
    session: Session,
    benchmark_id: str,
    page_size: int,
    group_by: str | None,
    secrets: tuple[str, ...],
) -> dict[str, _GroupAggregate]:
    groups: dict[str, _GroupAggregate] = {}
    offset = 0
    while True:
        rows = session.scalars(
            select(Question.metadata_)
            .where(Question.benchmark_id == benchmark_id)
            .order_by(Question.position, Question.id)
            .offset(offset)
            .limit(page_size)
        ).all()
        if not rows:
            break
        for metadata in rows:
            group_value = _question_group_value(metadata, group_by, secrets)
            groups.setdefault(group_value, _GroupAggregate()).planned_questions += 1
        offset += len(rows)
    if not groups:
        groups["all"] = _GroupAggregate()
    return groups


def _write_responses_jsonl(
    session: Session,
    run: EvaluationRun,
    path: Path,
    groups: dict[str, _GroupAggregate],
    group_by: str | None,
    page_size: int,
    secrets: tuple[str, ...],
) -> int:
    exported = 0
    offset = 0
    with _open_private_text(path) as output:
        while True:
            rows = session.execute(
                select(EvaluationResponse, Question)
                .join(Question, Question.id == EvaluationResponse.question_id)
                .where(EvaluationResponse.run_id == run.id)
                .order_by(Question.position, EvaluationResponse.id)
                .offset(offset)
                .limit(page_size)
            ).all()
            if not rows:
                break
            for response, question in rows:
                if question.benchmark_id != run.benchmark_id:
                    raise ReportIntegrityError(
                        "A persisted Response references a Question outside the Run Benchmark"
                    )
                group_value = _question_group_value(question.metadata_, group_by, secrets)
                aggregate = groups.get(group_value)
                if aggregate is None:
                    raise ReportIntegrityError("Response group is absent from planned questions")
                _accumulate_response(aggregate, response)
                payload = _response_payload(response, question, group_by, group_value, secrets)
                output.write(_json_dumps(payload))
                output.write("\n")
                exported += 1
            offset += len(rows)
        _flush_and_sync(output)
    return exported


def _accumulate_response(aggregate: _GroupAggregate, response: EvaluationResponse) -> None:
    aggregate.response_count += 1
    aggregate.score_sum += float(response.score)
    has_output = response.raw_response is not None and response.raw_response != ""
    if has_output:
        aggregate.completed_outputs += 1
    if response.error_type is None and has_output:
        aggregate.evaluable_responses += 1
    if response.error_type is not None:
        aggregate.error_questions += 1
    if response.latency_ms is not None:
        aggregate.latency_sum += float(response.latency_ms)
        aggregate.latency_reports += 1
    if response.input_tokens is not None:
        aggregate.input_tokens_sum += response.input_tokens
        aggregate.input_token_reports += 1
    if response.output_tokens is not None:
        aggregate.output_tokens_sum += response.output_tokens
        aggregate.output_token_reports += 1
    if response.estimated_cost is not None:
        aggregate.estimated_cost_sum += response.estimated_cost
        aggregate.estimated_cost_reports += 1


def _evidence_metrics(
    groups: Mapping[str, _GroupAggregate],
) -> dict[str, int | float | None]:
    aggregate = _GroupAggregate()
    for group in groups.values():
        aggregate.merge(group)
    return aggregate.as_metrics()


def _response_payload(
    response: EvaluationResponse,
    question: Question,
    group_by: str | None,
    group_value: str,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    payload = {
        "response_id": response.id,
        "run_id": response.run_id,
        "question_id": question.id,
        "question_external_id": question.external_id,
        "question_position": question.position,
        "question_type": question.question_type.value,
        "group_field": group_by,
        "group_value": group_value,
        "prompt": question.prompt,
        "choices": question.choices,
        "raw_response": response.raw_response,
        "parsed_answer": response.parsed_answer,
        "reference_answer_snapshot": response.reference_answer_snapshot,
        "score": float(response.score),
        "evaluator_name": response.evaluator_name,
        "latency_ms": response.latency_ms,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "estimated_cost": (
            float(response.estimated_cost) if response.estimated_cost is not None else None
        ),
        "provider_request_id": response.provider_request_id,
        "returned_model": response.returned_model,
        "system_fingerprint": response.system_fingerprint,
        "finish_reason": response.finish_reason,
        "http_attempt_count": response.http_attempt_count,
        "error_type": response.error_type,
        "error_message": response.error_message,
        "created_at": response.created_at,
    }
    sanitized = _sanitize_value(payload, secrets)
    for field in ("provider_request_id", "returned_model", "system_fingerprint"):
        sanitized[field] = normalize_provider_metadata(sanitized[field], max_length=256)
    sanitized["finish_reason"] = normalize_provider_metadata(
        sanitized["finish_reason"], max_length=128
    )
    sanitized["http_attempt_count"] = normalize_http_attempt_count(sanitized["http_attempt_count"])
    return sanitized


def _write_groups_csv(
    path: Path,
    groups: Mapping[str, _GroupAggregate],
    group_by: str | None,
) -> int:
    fieldnames = [
        "group_field",
        "group_value",
        "planned_questions",
        "response_count",
        "correct_questions",
        "error_questions",
        "score",
        "completion_rate",
        "answered_accuracy",
    ]
    with _open_private_text(path, newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for group_value in sorted(groups):
            row = groups[group_value].as_row(group_by, group_value)
            row["group_value"] = _safe_csv_cell(str(row["group_value"]))
            writer.writerow(row)
        _flush_and_sync(output)
    return len(groups)


def _build_summary(
    run: EvaluationRun,
    model: Model,
    benchmark: Benchmark,
    group_by: str | None,
    evidence_metrics: Mapping[str, int | float | None],
    response_count: int,
    group_count: int,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    snapshot = dict(run.model_parameters_snapshot or {})
    model_snapshot = dict(snapshot.get("model", {}))
    benchmark_snapshot = dict(snapshot.get("benchmark", {}))
    persisted_metrics = _persisted_run_metrics(run)
    metric_differences = [
        name
        for name, value in evidence_metrics.items()
        if not _metric_values_equal(value, persisted_metrics[name])
    ]
    summary = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "run": {
            "id": run.id,
            "status": run.status.value,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        },
        "model": {
            "id": run.model_id,
            "name": model_snapshot.get("name") or model.name,
            "provider_type": model_snapshot.get("adapter_type") or model.provider_type.value,
            "remote_model_name": model_snapshot.get("remote_model_name"),
        },
        "benchmark": {
            "id": run.benchmark_id,
            "slug": benchmark_snapshot.get("slug") or benchmark.slug,
            "name": benchmark_snapshot.get("name") or benchmark.name,
            "version": benchmark_snapshot.get("version") or benchmark.version,
            "question_count": run.total_questions,
        },
        "protocol_version": run.protocol_version,
        "dataset_hash": run.benchmark_hash_snapshot,
        "code_commit_sha": run.code_commit_sha,
        "metrics": dict(evidence_metrics),
        "metrics_provenance": {
            "source": "persisted_responses_and_planned_questions",
            "persisted_run_fields_consistent": not metric_differences,
            "persisted_run_field_differences": metric_differences,
        },
        "snapshots": {
            "model_parameters": run.model_parameters_snapshot,
            "prompt_template": run.prompt_template_snapshot,
        },
        "group_by": group_by,
        "files": {
            "summary": {
                "path": SUMMARY_FILENAME,
                "line_count": 1,
                "record_count": 1,
            },
            "groups": {
                "path": GROUPS_FILENAME,
                "line_count": group_count + 1,
                "record_count": group_count,
            },
            "responses": {
                "path": RESPONSES_FILENAME,
                "line_count": response_count,
                "record_count": response_count,
            },
        },
    }
    return _sanitize_value(summary, secrets)


def _persisted_run_metrics(run: EvaluationRun) -> dict[str, int | float | None]:
    return {
        "total_questions": run.total_questions,
        "completed_questions": run.completed_questions,
        "correct_questions": run.correct_questions,
        "error_questions": run.error_questions,
        "score": run.score,
        "completion_rate": run.completion_rate,
        "answered_accuracy": run.answered_accuracy,
        "average_latency_ms": run.average_latency_ms,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "estimated_cost": (float(run.estimated_cost) if run.estimated_cost is not None else None),
    }


def _metric_values_equal(left: int | float | None, right: int | float | None) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, float) or isinstance(right, float):
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    return left == right


def _question_group_value(
    metadata: object,
    group_by: str | None,
    secrets: tuple[str, ...],
) -> str:
    if group_by is None:
        return "all"
    values = metadata if isinstance(metadata, Mapping) else {}
    raw_value = _metadata_group_value(values.get(group_by))
    if raw_value is None:
        return "__ungrouped__"
    return _sanitize_text(raw_value, secrets)


def _metadata_group_value(value: object) -> str | None:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float, Decimal)):
        normalized = str(value).strip()
        return normalized or None
    return None


def _sanitize_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            normalized_key = re.sub(r"[^a-z0-9]", "", key_text.lower())
            if normalized_key in _SENSITIVE_MAPPING_KEYS:
                continue
            sanitized[_sanitize_text(key_text, secrets)] = _sanitize_value(nested, secrets)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(item, secrets) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, secrets)
    if isinstance(value, datetime):
        return _iso_datetime(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_text(str(value), secrets)


def _sanitize_text(value: str, secrets: tuple[str, ...]) -> str:
    sanitized = value
    for secret in secrets:
        sanitized = sanitized.replace(secret, "[REDACTED]")
    sanitized = _AUTHORIZATION_RE.sub("[REDACTED]", sanitized)
    sanitized = _BEARER_RE.sub("[REDACTED]", sanitized)
    sanitized = _SECRET_ASSIGNMENT_RE.sub("[REDACTED]", sanitized)
    sanitized = _COMMON_TOKEN_RE.sub("[REDACTED]", sanitized)
    return sanitized


def _iso_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_csv_cell(value: str) -> str:
    if value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _write_json_line(path: Path, value: Any) -> None:
    with _open_private_text(path) as output:
        output.write(_json_dumps(value))
        output.write("\n")
        _flush_and_sync(output)


def _open_private_text(path: Path, *, newline: str | None = None):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.chmod(path, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline=newline)


def _flush_and_sync(output: Any) -> None:
    output.flush()
    os.fsync(output.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
