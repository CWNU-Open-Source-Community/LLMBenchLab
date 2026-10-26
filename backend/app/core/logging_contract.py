"""Closed, non-reflective value contract for serialized application logs."""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime
from uuid import UUID

OMIT = object()

_APPLICATION_LOGGER_NAMES = frozenset(
    {
        "app.api.v1.governance",
        "app.api.v1.health",
        "app.api.v1.observability",
        "app.api.v1.runs",
        "app.main",
        "app.runners.evaluation_runner",
        "app.worker",
        "app.worker_progress",
        "app.workers.service",
    }
)
_APPLICATION_MESSAGE = "Application log event"
_UNSUPPORTED = "unsupported"
_UNSUPPORTED_ROUTE = "<unsupported>"
_LEVELS = frozenset({"CRITICAL", "DEBUG", "ERROR", "INFO", "WARNING"})
_APPLICATION_MESSAGES = frozenset(
    {
        "API request completed",
        "API request failed",
        "Discarding invalid Run queue notification",
        "Evaluation adapter cleanup failed",
        "Evaluation run drained for shutdown; durable lease expiry will recover it",
        "Evaluation run failed",
        "Evaluation run failure transition resolved",
        "Evaluation run finish transition resolved",
        "Evaluation run governance transition resolved",
        "Evaluation run interrupted; durable lease expiry will recover it",
        "Evaluation run question quantum yielded",
        "Evaluation run stopped after cancellation",
        "Evaluation run stopped after losing its lease",
        "Evaluator failed for question",
        "Expired Run reconciliation completed",
        "Expired Run reconciliation failed governance integrity validation",
        "Governance integrity evidence could not be recorded",
        "Metrics audit event failed read validation",
        "Metrics audit observation limit exceeded",
        "Metrics database collection failed",
        "Metrics queue observation failed",
        "Metrics queue observation unavailable",
        "Metrics rendering failed",
        "Question evidence persistence resolved",
        "Question processing failed",
        "Retained Run audit event failed read validation",
        "Retained task history audit event failed read validation",
        "Run claim was a durable no-op",
        "Run lease claimed",
        "Run lease heartbeat failed",
        "Run lease heartbeat rejected",
        "Run lease heartbeat renewed",
        "Run persisted before queue notification",
        "Run queue ACK observed",
        "Run queue connection recovered",
        "Run queue failure evidence could not be recorded",
        "Run queue notification published",
        "Run queue notification unavailable",
        "Run queue success evidence could not be recorded",
        "Worker database connection recovered",
        "Worker database unavailable; task delivery is paused",
        "Worker finished Run handling",
        "Worker main loop registered and started",
        "Worker main service stopped before a healthy lifecycle completed",
        "Worker process is starting",
        "Worker progress flush failed; retained for retry",
        "Worker progress graceful stop failed; generation will become stale",
        "Worker progress observer rejected an execution fact",
        "Worker received Run queue delivery",
        "Worker Run task escaped its isolation boundary",
        "Worker shutdown grace expired; active Run was interrupted",
        "Worker started Run handling",
        "Worker startup database check failed",
        "Worker stopped",
        "Run queue unavailable; database reconciliation remains active",
    }
)
_HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_ROUTE_TEMPLATES = frozenset(
    {
        "<unmatched>",
        "/benchmarks",
        "/benchmarks/import",
        "/benchmarks/reload-demo",
        "/benchmarks/{benchmark_id}",
        "/benchmarks/{benchmark_id}/questions",
        "/docs",
        "/docs/oauth2-redirect",
        "/governance/policy",
        "/health",
        "/info",
        "/leaderboard",
        "/live",
        "/metrics/prometheus",
        "/metrics/summary",
        "/models",
        "/models/{model_id}",
        "/openapi.json",
        "/ready",
        "/redoc",
        "/runs",
        "/runs/{run_id}",
        "/runs/{run_id}/audit",
        "/runs/{run_id}/cancel",
        "/runs/{run_id}/progress",
        "/runs/{run_id}/progress/blocks/{block_index}",
        "/runs/{run_id}/responses",
        "/tasks/history",
        "/tasks/metrics",
    }
)
_EVENTS = frozenset(
    {
        "api_request_completed",
        "api_request_failed",
        "governance_integrity_audit_failed",
        "metrics_audit_integrity_error",
        "metrics_database_unavailable",
        "metrics_observation_limit_exceeded",
        "metrics_queue_check_failed",
        "metrics_queue_unavailable",
        "metrics_rendering_failed",
        "question_evaluator_failed",
        "question_evidence_persisted",
        "question_processing_failed",
        "run_adapter_cleanup_failed",
        "run_attempt_failed",
        "run_attempt_failure_resolved",
        "run_attempt_finished",
        "run_audit_integrity_error",
        "run_cancelled",
        "run_claim_noop",
        "run_claimed",
        "run_created",
        "run_governance_transition_resolved",
        "run_heartbeat_failed",
        "run_heartbeat_rejected",
        "run_heartbeat_renewed",
        "run_lease_lost",
        "run_question_quantum_yielded",
        "run_queue_ack_failed",
        "run_queue_ack_observed",
        "run_queue_audit_failed",
        "run_queue_delivery_received",
        "run_queue_invalid_message",
        "run_queue_publish_failed",
        "run_queue_published",
        "run_queue_read_failed",
        "run_queue_recovered",
        "run_shutdown_lease_expiry",
        "task_history_audit_integrity_error",
        "worker_database_recovered",
        "worker_main_loop_started",
        "worker_progress_flush_failed",
        "worker_progress_observer_failed",
        "worker_progress_stop_failed",
        "worker_reap_database_unavailable",
        "worker_reap_governance_integrity_error",
        "worker_reap_outcome",
        "worker_run_finished",
        "worker_run_started",
        "worker_run_unhandled_error",
        "worker_scan_database_unavailable",
        "worker_service_failed",
        "worker_shutdown_interrupted",
        "worker_start_failed",
        "worker_starting",
        "worker_stopped",
    }
)
_RESULTS = frozenset(
    {
        "ack_safe",
        "acknowledged",
        "already_absent",
        "already_present",
        "cancel_requested",
        "cancelled",
        "claimed",
        "completed",
        "cooperative_yield",
        "dead_lettered",
        "drained",
        "error_response",
        "failed",
        "fence_lost",
        "fenced",
        "governance_deferred",
        "governance_exhausted",
        "ignored",
        "ignored_after_cleanup_attempt",
        "inserted",
        "internal_server_error",
        "interrupted",
        "lease_expiry_recovery",
        "lease_lost",
        "not_acknowledged",
        "not_claimable",
        "not_found",
        "not_recorded",
        "observed_unavailable",
        "paused",
        "pending",
        "published",
        "recovered_completed",
        "rejected",
        "renewed",
        "requested",
        "retained",
        "retry_or_dead_letter",
        "retry_scheduled",
        "running",
        "stale",
        "stopped",
        "terminal",
        "unavailable",
    }
)
_ERROR_CODES = frozenset(
    {
        "audit_event_integrity_error",
        "authentication_error",
        "connect_timeout",
        "empty_response",
        "evaluator_internal_error",
        "incomplete_provider_stream",
        "invalid_provider_response",
        "invalid_provider_stream",
        "invalid_request",
        "metrics_database_unavailable",
        "metrics_observation_limit_exceeded",
        "metrics_rendering_error",
        "missing_api_key",
        "mock_configuration_error",
        "mock_error",
        "network_error",
        "network_timeout",
        "none",
        "output_truncated",
        "parse_error",
        "provider_4xx",
        "provider_5xx",
        "provider_http_error",
        "provider_response_too_large",
        "provider_stream_error",
        "question_internal_error",
        "queue_check_unavailable",
        "queue_unavailable",
        "rate_limited",
        "read_timeout",
        "unsupported_provider_response_encoding",
    }
)
_EXCEPTION_ERROR_FAMILIES = frozenset(
    {
        "adapter_cleanup_error",
        "api_error",
        "evaluator_internal_error",
        "question_internal_error",
        "runner_error",
        "worker_progress_error",
        "worker_run_error",
        "worker_service_error",
    }
)
_GOVERNANCE_ERROR_CODES = frozenset(
    {
        "governance_active_policy_missing",
        "governance_database_clock_reversed",
        "governance_input_bound_unknown",
        "governance_lease_fence_lost",
        "governance_minute_bucket_counter_drift",
        "governance_minute_bucket_missing",
        "governance_multiple_active_policies",
        "governance_policy_hash_mismatch",
        "governance_policy_missing",
        "governance_pricing_unknown",
        "governance_provider_retry_exhausted",
        "governance_question_execution_missing",
        "governance_question_retry_cursor_mismatch",
        "governance_release_after_send",
        "governance_reservation_missing",
        "governance_run_override_snapshot_mismatch",
        "governance_run_policy_missing",
        "governance_run_snapshot_mismatch",
        "governance_scope_counter_drift",
        "governance_scope_missing",
        "governance_settle_before_send",
        "governance_settlement_unknown",
        "governance_unbounded_output",
    }
    | {
        f"governance_{scope}_{reason}"
        for scope in ("global", "model", "provider", "run")
        for reason in ("concurrency", "overdrawn", "rpm", "tpm")
    }
    | {
        f"governance_{scope}_{reason}_budget_exhausted"
        for scope in ("global", "run")
        for reason in ("cost", "request", "token")
    }
)
_COMPONENTS = frozenset({"api", "database", "governance", "metrics", "redis", "worker"})
_REDIS_STREAM_ID = re.compile(r"(0|[1-9][0-9]{0,19})-(0|[1-9][0-9]{0,19})")
_WORKER_ID = re.compile(
    r"worker:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,39}):[1-9][0-9]{0,9}:"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
_REAP_RESULT = re.compile(
    r"cancelled=(0|[1-9][0-9]{0,9}),completed=(0|[1-9][0-9]{0,9}),"
    r"dead_lettered=(0|[1-9][0-9]{0,9}),retry_scheduled=(0|[1-9][0-9]{0,9})"
)
_INT32_MAX = 2**31 - 1
_INT64_MAX = 2**63 - 1
_UINT64_MAX = 2**64 - 1


def is_application_logger(name: object) -> bool:
    return isinstance(name, str) and name in _APPLICATION_LOGGER_NAMES


def normalize_uuid_identifier(value: object) -> str | None:
    """Return a canonical UUID or ``None`` without reflecting invalid input."""

    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError):
        return None
    return normalized if value.lower() == normalized else None


def normalize_correlation_id(value: object) -> str | None:
    return normalize_uuid_identifier(value)


def normalize_redis_stream_id(value: object) -> str | None:
    """Return a canonical Redis stream entry ID or ``None``."""

    if not isinstance(value, str):
        return None
    match = _REDIS_STREAM_ID.fullmatch(value)
    if match is None:
        return None
    milliseconds, sequence = (int(part) for part in match.groups())
    if milliseconds > _UINT64_MAX or sequence > _UINT64_MAX:
        return None
    return f"{milliseconds}-{sequence}"


def normalize_request_method(value: object) -> str:
    return value if isinstance(value, str) and value in _HTTP_METHODS else _UNSUPPORTED


def normalize_request_path(value: object) -> str:
    return value if isinstance(value, str) and value in _ROUTE_TEMPLATES else _UNSUPPORTED_ROUTE


def normalize_log_level(value: object) -> str:
    return value if isinstance(value, str) and value in _LEVELS else "UNKNOWN"


def normalize_timestamp(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "1970-01-01T00:00:00+00:00"
    try:
        if not math.isfinite(value):
            return "1970-01-01T00:00:00+00:00"
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return "1970-01-01T00:00:00+00:00"


def normalize_application_message(record: logging.LogRecord) -> str:
    if not (isinstance(record.args, tuple) and not record.args):
        return _APPLICATION_MESSAGE
    if isinstance(record.msg, str) and record.msg in _APPLICATION_MESSAGES:
        return record.msg
    return _APPLICATION_MESSAGE


def _normalize_nonnegative_int(value: object, *, maximum: int) -> int | object:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        return OMIT
    return value


def _normalize_duration(value: object) -> int | float | object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return OMIT
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return OMIT
    if not finite or not 0 <= value <= _INT64_MAX:
        return OMIT
    return value


def _normalize_worker_id(value: object) -> str | object:
    if not isinstance(value, str):
        return OMIT
    match = _WORKER_ID.fullmatch(value)
    if match is None or normalize_uuid_identifier(match.group(1)) is None:
        return OMIT
    # Hostnames and PIDs are process-controlled but may still carry local
    # metadata. Preserve only the random generation suffix in serialized logs.
    return f"worker:{match.group(1)}"


def _normalize_result(value: object) -> str:
    if isinstance(value, str) and value in _RESULTS:
        return value
    if isinstance(value, str):
        match = _REAP_RESULT.fullmatch(value)
        if match is not None and all(int(part) <= _INT32_MAX for part in match.groups()):
            return value
    return _UNSUPPORTED


def _normalize_error_code(value: object) -> str:
    if not isinstance(value, str):
        return _UNSUPPORTED
    if value in _ERROR_CODES or value in _GOVERNANCE_ERROR_CODES:
        return value
    family, separator, _detail = value.partition(":")
    if separator and family in _EXCEPTION_ERROR_FAMILIES:
        # Exception class names are useful locally but cannot be proven free of
        # attacker-selected text at the final serialization boundary.
        return family
    return _UNSUPPORTED


def normalize_structured_field(field: str, value: object) -> object:
    if field == "event":
        return value if isinstance(value, str) and value in _EVENTS else _UNSUPPORTED
    if field in {"run_id", "question_id"}:
        return normalize_uuid_identifier(value) or OMIT
    if field == "worker_id":
        return _normalize_worker_id(value)
    if field == "attempt":
        return _normalize_nonnegative_int(value, maximum=_INT32_MAX)
    if field == "lease_token":
        return _normalize_nonnegative_int(value, maximum=_INT64_MAX)
    if field == "message_id":
        return normalize_redis_stream_id(value) or OMIT
    if field == "request_method":
        return normalize_request_method(value)
    if field == "request_path":
        return normalize_request_path(value)
    if field == "status_code":
        normalized = _normalize_nonnegative_int(value, maximum=599)
        return normalized if normalized is not OMIT and normalized >= 100 else OMIT
    if field == "duration_ms":
        return _normalize_duration(value)
    if field == "result":
        return _normalize_result(value)
    if field == "error_code":
        return _normalize_error_code(value)
    if field == "component":
        return value if isinstance(value, str) and value in _COMPONENTS else _UNSUPPORTED
    return OMIT
