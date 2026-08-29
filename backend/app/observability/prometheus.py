"""Strict Prometheus text exposition for the fixed Phase 2 metric set."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import datetime

from app.observability.snapshot import (
    METRICS_AUDIT_EVENT_LIMIT,
    METRICS_AUDIT_WINDOW_SECONDS,
    METRICS_LATENCY_SAMPLE_LIMIT,
    METRICS_LATENCY_WINDOW_SECONDS,
    TASK_EVENT_TYPES,
    LatencySnapshot,
    OperationalSnapshot,
    as_utc,
)

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class PrometheusRenderingError(RuntimeError):
    """Raised when a snapshot cannot be represented without invalid samples."""


_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_FIXED_LABEL_VALUES = {
    "event_type": frozenset(TASK_EVENT_TYPES),
    "phase": frozenset({"queue", "execution", "end_to_end"}),
    "quantile": frozenset({"0.5", "0.95", "0.99"}),
    "state": frozenset({"registered", "live", "stalled"}),
    "activity": frozenset({"scan", "claim", "lease_heartbeat", "progress"}),
}


def _number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PrometheusRenderingError("metrics_value_type_invalid")
    if isinstance(value, int):
        if value < 0:
            raise PrometheusRenderingError("metrics_value_not_finite_nonnegative")
        return str(value)
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise PrometheusRenderingError("metrics_value_not_finite_nonnegative")
    if numeric == 0:
        return "0"
    return format(numeric, ".15g")


def _labels(values: tuple[tuple[str, str], ...]) -> str:
    if not values:
        return ""
    rendered: list[str] = []
    for name, value in values:
        allowed = _FIXED_LABEL_VALUES.get(name)
        if (
            _LABEL_NAME.fullmatch(name) is None
            or allowed is None
            or value not in allowed
            or any(character in value for character in ('"', "\\", "\n", "\r"))
        ):
            raise PrometheusRenderingError("metrics_label_invalid")
        rendered.append(f'{name}="{value}"')
    return "{" + ",".join(rendered) + "}"


def _family(
    lines: list[str],
    *,
    name: str,
    help_text: str,
    samples: Iterable[tuple[tuple[tuple[str, str], ...], object]],
) -> None:
    if _METRIC_NAME.fullmatch(name) is None or "\n" in help_text or "\r" in help_text:
        raise PrometheusRenderingError("metrics_metadata_invalid")
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    for labels, value in samples:
        lines.append(f"{name}{_labels(labels)} {_number(value)}")


def _scalar(lines: list[str], name: str, help_text: str, value: object) -> None:
    _family(lines, name=name, help_text=help_text, samples=(((), value),))


def _latency_value(snapshot: LatencySnapshot, quantile: str) -> float | None:
    if quantile == "0.5":
        value = snapshot.p50_ms
    elif quantile == "0.95":
        value = snapshot.p95_ms
    else:
        value = snapshot.p99_ms
    return None if value is None else value / 1000


def _activity_age(now: datetime, observed_at: datetime | None) -> float | None:
    if observed_at is None:
        return None
    age = (as_utc(now) - as_utc(observed_at)).total_seconds()
    if age < 0:
        raise PrometheusRenderingError("metrics_worker_activity_in_future")
    return age


def render_prometheus(
    snapshot: OperationalSnapshot,
    *,
    queue_configured: bool,
    queue_available: bool,
    recovery_alert_seconds: float,
) -> str:
    """Render one fixed-order, low-cardinality exposition with no dynamic labels."""

    current = snapshot.current
    history = snapshot.history
    lines: list[str] = []

    current_metrics = (
        ("llmbenchlab_runs_pending", "Pending evaluation Runs.", current.pending),
        (
            "llmbenchlab_runs_due_pending",
            "Pending evaluation Runs currently eligible for dispatch.",
            current.due_pending,
        ),
        ("llmbenchlab_runs_running", "Running evaluation Runs.", current.running),
        (
            "llmbenchlab_runs_expired_running",
            "Running evaluation Runs whose database lease has expired.",
            current.expired_running,
        ),
        (
            "llmbenchlab_runs_cancellation_requested",
            "Active Runs with cancellation requested.",
            current.active_cancellation_requests,
        ),
        (
            "llmbenchlab_runs_retry_scheduled",
            "Pending Runs with a database retry timestamp.",
            current.retry_scheduled,
        ),
        (
            "llmbenchlab_runs_dead_lettered",
            "Runs currently carrying a dead-letter timestamp.",
            current.dead_lettered,
        ),
        (
            "llmbenchlab_runs_queue_notification_error",
            "Runs carrying the fixed queue-notification-unavailable error.",
            current.runs_with_queue_notification_error,
        ),
        (
            "llmbenchlab_runs_managed_backlog",
            "Active managed Runs in the database backlog.",
            current.managed_backlog,
        ),
        (
            "llmbenchlab_runs_governance_delayed",
            "Runs currently delayed by governance.",
            current.governance_delayed,
        ),
        (
            "llmbenchlab_runs_governance_exhausted",
            "Runs currently exhausted by governance.",
            current.governance_exhausted,
        ),
        (
            "llmbenchlab_provider_attempts_active",
            "Provider attempt reservations not yet settled or released.",
            current.active_provider_attempts,
        ),
        (
            "llmbenchlab_governance_scopes_overdrawn",
            "Governance scopes marked overdrawn.",
            current.overdrawn_governance_scopes,
        ),
        (
            "llmbenchlab_run_lease_acquisitions",
            "Persisted Run lease acquisition count projection.",
            current.total_attempts,
        ),
        (
            "llmbenchlab_run_failed_attempts",
            "Persisted failed Run attempt count projection.",
            current.total_failed_attempts,
        ),
        (
            "llmbenchlab_run_dispatches",
            "Persisted Run dispatch count projection.",
            current.total_dispatches,
        ),
        (
            "llmbenchlab_run_expired_lease_oldest_age_seconds",
            "Right-censored age of the oldest currently expired running lease.",
            current.expired_lease_oldest_age_seconds,
        ),
    )
    for name, help_text, value in current_metrics:
        _scalar(lines, name, help_text, value)

    _family(
        lines,
        name="llmbenchlab_audit_events_window",
        help_text="Validated retained audit events in the fixed rolling window.",
        samples=(
            ((("event_type", event_type),), history.event_counts[event_type])
            for event_type in TASK_EVENT_TYPES
        ),
    )
    _scalar(
        lines,
        "llmbenchlab_audit_event_window_seconds",
        "Fixed rolling audit-event observation window.",
        METRICS_AUDIT_WINDOW_SECONDS,
    )
    _scalar(
        lines,
        "llmbenchlab_metrics_audit_events_scanned",
        "Retained audit rows validated during this collection.",
        history.audit_events_scanned,
    )
    _scalar(
        lines,
        "llmbenchlab_metrics_audit_event_limit",
        "Hard retained audit row limit for one collection.",
        METRICS_AUDIT_EVENT_LIMIT,
    )

    latency_snapshots = (
        ("queue", history.queue_latency),
        ("execution", history.execution_latency),
        ("end_to_end", history.end_to_end_latency),
    )
    quantile_samples = []
    for phase, latency in latency_snapshots:
        for quantile in ("0.5", "0.95", "0.99"):
            value = _latency_value(latency, quantile)
            if value is not None:
                quantile_samples.append(((("phase", phase), ("quantile", quantile)), value))
    _family(
        lines,
        name="llmbenchlab_run_latency_quantile_seconds",
        help_text="Run latency quantile over the fixed latency window.",
        samples=quantile_samples,
    )
    _family(
        lines,
        name="llmbenchlab_run_latency_samples",
        help_text="Run latency samples used after the stable sample cap.",
        samples=(
            ((("phase", phase),), latency.sample_count) for phase, latency in latency_snapshots
        ),
    )
    _family(
        lines,
        name="llmbenchlab_run_latency_truncated",
        help_text="Whether the Run latency sample page was truncated.",
        samples=(
            ((("phase", phase),), int(latency.truncated)) for phase, latency in latency_snapshots
        ),
    )
    _scalar(
        lines,
        "llmbenchlab_run_latency_window_seconds",
        "Fixed Run latency observation window.",
        METRICS_LATENCY_WINDOW_SECONDS,
    )
    _scalar(
        lines,
        "llmbenchlab_metrics_latency_sample_limit",
        "Hard sample limit for each Run latency phase.",
        METRICS_LATENCY_SAMPLE_LIMIT,
    )

    _scalar(
        lines,
        "llmbenchlab_queue_configured",
        "Whether a Redis notification queue is configured.",
        int(queue_configured),
    )
    _scalar(
        lines,
        "llmbenchlab_queue_available",
        "Whether the configured Redis notification queue answered this scrape.",
        int(queue_available),
    )
    worker = current.worker
    _family(
        lines,
        name="llmbenchlab_worker_processes",
        help_text="Registered Worker generations by derived database-time state.",
        samples=(
            ((("state", "registered"),), worker.registered),
            ((("state", "live"),), worker.live),
            ((("state", "stalled"),), worker.stalled),
        ),
    )
    _scalar(
        lines,
        "llmbenchlab_worker_expected_minimum",
        "Configured minimum number of live Worker processes.",
        worker.expected,
    )
    _scalar(
        lines,
        "llmbenchlab_worker_shortfall",
        "Configured live Worker process shortfall.",
        worker.shortfall,
    )
    oldest_activity = current.worker_activity_oldest
    activities = (
        ("scan", oldest_activity.scan_at),
        ("claim", oldest_activity.claim_at),
        ("lease_heartbeat", oldest_activity.lease_heartbeat_at),
        ("progress", oldest_activity.progress_at),
    )
    activity_ages = tuple(
        (activity, _activity_age(current.timestamp, observed_at))
        for activity, observed_at in activities
    )
    _family(
        lines,
        name="llmbenchlab_worker_activity_observed",
        help_text="Whether an active Worker has recorded this activity.",
        samples=(
            ((("activity", activity),), int(age is not None)) for activity, age in activity_ages
        ),
    )
    _family(
        lines,
        name="llmbenchlab_worker_activity_oldest_age_seconds",
        help_text="Database-time age of the oldest active-generation Worker activity fact.",
        samples=(
            ((("activity", activity),), age) for activity, age in activity_ages if age is not None
        ),
    )
    _scalar(
        lines,
        "llmbenchlab_worker_stale_threshold_seconds",
        "Configured database-time Worker stale threshold.",
        worker.stale_seconds,
    )
    _scalar(
        lines,
        "llmbenchlab_run_recovery_alert_threshold_seconds",
        "Configured alert threshold for an expired running lease.",
        recovery_alert_seconds,
    )
    _scalar(
        lines,
        "llmbenchlab_metrics_snapshot_unixtime_seconds",
        "Database snapshot timestamp as Unix seconds.",
        as_utc(current.timestamp).timestamp(),
    )

    rendered = "\n".join(lines) + "\n"
    if "\r" in rendered or not rendered.endswith("\n") or rendered.endswith("\n\n"):
        raise PrometheusRenderingError("metrics_text_framing_invalid")
    return rendered
