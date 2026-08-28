#!/usr/bin/env python3
"""Run a bounded, Mock-only Phase 2 capacity baseline on isolated Compose.

The harness uses PostgreSQL 16, Redis 7, and at least two independent Workers.
It applies an explicit finite governance policy and records machine-readable,
sanitized evidence for worker scaling, exact local backlog admission, fair
cross-Model slices, lease expiry recovery, Redis stop/start, duplicate delivery,
and governance ledger/audit reconciliation. Results describe only the recorded
commit, host, container limits, and Mock configuration; they are not an SLO,
Provider-side admission result, billing statement, or SLA.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import math
import os
import platform
import signal
import statistics
import sys
import time
import traceback
import uuid
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from phase2_acceptance import (  # noqa: E402
    PROJECT_PATTERN,
    RUN_SNAPSHOT_FIELDS,
    TASK_MESSAGE_VERSION,
    TASK_STREAM,
    AcceptanceFailure,
    AcceptanceInterrupted,
    Phase2Acceptance,
    parse_datetime,
    redact_text,
    sanitize,
    utc_now,
)

CAPACITY_EVIDENCE_SCHEMA_V1 = "llmbenchlab-phase2-capacity-evidence-v1"
CAPACITY_EVIDENCE_SCHEMA_V2 = "llmbenchlab-phase2-capacity-evidence-v2"
EVIDENCE_SCHEMA = CAPACITY_EVIDENCE_SCHEMA_V1
DEFAULT_QUALIFICATION_PROFILE = "capacity-v1"
FORMAL_QUALIFICATION_PROFILE = "P2-local-control-plane-v2"
QUALIFICATION_PROFILES = (
    DEFAULT_QUALIFICATION_PROFILE,
    FORMAL_QUALIFICATION_PROFILE,
)
FORMAL_V2_ARGUMENTS: dict[str, int | float] = {
    "workers": 2,
    "runs_per_phase": 4,
    "backlog_limit": 4,
    "burst_runs": 6,
    "submit_concurrency": 6,
    "run_concurrency": 1,
    "question_quantum": 5,
    "mock_delay_seconds": 0.08,
    "timeout_seconds": 180,
    "lease_seconds": 30,
    "heartbeat_seconds": 10,
    "worker_poll_seconds": 1,
    "worker_max_attempts": 3,
    "retry_backoff_base_seconds": 1,
    "retry_backoff_cap_seconds": 30,
    "worker_shutdown_grace_seconds": 30,
    "redis_block_milliseconds": 1000,
    "redis_operation_timeout_seconds": 1,
}
DEFAULT_ARTIFACTS_ROOT = Path(".pytest_cache/artifacts/phase2-capacity")
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
DEMO_QUESTIONS_PER_RUN = 15
RUN_MAX_TOKENS = 64
RUN_INPUT_TOKEN_RESERVATION = 256
RUN_LIFETIME_REQUEST_BUDGET = 100
RUN_LIFETIME_TOKEN_BUDGET = 100_000
RUN_LIFETIME_COST_BUDGET_USD = "100.00000000"
DATABASE_POOL_SIZE = 5
DATABASE_MAX_OVERFLOW = 5
DATABASE_POOL_TIMEOUT_SECONDS = 2.0
READINESS_DATABASE_TIMEOUT_SECONDS = 2.0
WORKER_MAX_ATTEMPTS = 3
WORKER_RETRY_BACKOFF_BASE_SECONDS = 1.0
WORKER_RETRY_BACKOFF_CAP_SECONDS = 30.0
WORKER_SHUTDOWN_GRACE_SECONDS = 30.0
REDIS_BLOCK_MILLISECONDS = 1000
REDIS_OPERATION_TIMEOUT_SECONDS = 1.0
PROVIDER_CREDENTIAL_ENV_KEYS = (
    "OPENAI_API_KEY",
    "LLMBENCHLAB_DEMO_API_KEY",
    "LLMBENCHLAB_REAL_API_KEY",
    "TEST_PROVIDER_KEY",
)
COMPOSE_PROJECT_IMAGE_LABELS = frozenset(
    {
        "com.docker.compose.project",
        "com.docker.compose.service",
    }
)


def _is_sha256_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _is_container_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def image_content_sha256(inspected_image: dict[str, Any]) -> str:
    """Fingerprint executable image content without per-project Compose labels."""

    rootfs = inspected_image.get("RootFS")
    config = inspected_image.get("Config")
    if not isinstance(rootfs, dict) or not isinstance(config, dict):
        raise AcceptanceFailure("docker image inspect omitted RootFS or Config")
    layers = rootfs.get("Layers")
    if (
        not isinstance(layers, list)
        or not layers
        or not all(_is_sha256_identity(layer) for layer in layers)
    ):
        raise AcceptanceFailure("docker image inspect returned invalid RootFS layers")
    stable_config = dict(config)
    labels = config.get("Labels")
    if labels is not None:
        if not isinstance(labels, dict):
            raise AcceptanceFailure("docker image inspect returned invalid Config.Labels")
        stable_config["Labels"] = {
            key: value for key, value in labels.items() if key not in COMPOSE_PROJECT_IMAGE_LABELS
        }
    payload = {
        "architecture": inspected_image.get("Architecture"),
        "os": inspected_image.get("Os"),
        "variant": inspected_image.get("Variant"),
        "rootfs_layers": layers,
        "config": stable_config,
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AcceptanceFailure("docker image content was not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def reconciliation_expectations(
    *,
    runs_per_phase: int,
    backlog_limit: int,
    formal_slo_v2: bool = False,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Derive terminal ledger counts from the accepted capacity workload."""

    backlog_groups = 3 if formal_slo_v2 else 2
    completed_runs = 2 * runs_per_phase + backlog_groups * backlog_limit + 2
    settled_actual = completed_runs * DEMO_QUESTIONS_PER_RUN
    reservations = settled_actual + 1
    return (
        {
            "policies": 2,
            "active_policies": 1,
            "runs": completed_runs,
            "responses": settled_actual,
            "distinct_run_question_responses": settled_actual,
            "question_executions": settled_actual,
            "reservations": reservations,
            "failed_attempt_count": 1,
            "question_error_count": 0,
        },
        {
            "settled_actual": settled_actual,
            "settled_conservative": 1,
        },
        {
            "provider_attempt_reserved": reservations,
            "provider_attempt_send_started": reservations,
            "provider_attempt_settled": reservations,
        },
    )


(
    EXPECTED_RECONCILIATION_COUNTS,
    EXPECTED_RESERVATION_STATES,
    EXPECTED_PROVIDER_ATTEMPT_AUDIT_COUNTS,
) = reconciliation_expectations(runs_per_phase=4, backlog_limit=4)
RECONCILIATION_ZERO_FIELDS = (
    "active_reservations",
    "scope_active_reservations",
    "scope_reserved_requests",
    "scope_reserved_input_tokens",
    "scope_reserved_output_tokens",
    "minute_reserved_requests",
    "minute_reserved_input_tokens",
    "minute_reserved_output_tokens",
    "overdrawn_scopes",
    "duplicate_operation_keys",
    "duplicate_response_questions",
    "duplicate_audit_event_keys",
    "active_runs",
    "missing_scope_projection_rows",
    "extra_scope_projection_rows",
    "scope_projection_field_drift",
    "missing_minute_projection_rows",
    "extra_minute_projection_rows",
    "minute_projection_field_drift",
)

# This deliberately mirrors GovernanceRepository._scope_fact_source() and
# _scope_fact_aggregates(). The acceptance query remains independent of the ORM
# projection so a bug in projection maintenance cannot validate itself.
GOVERNANCE_RECONCILIATION_SQL = """
WITH scope_facts AS MATERIALIZED (
  SELECT
    scope_ids.scope_id,
    reservation.policy_id,
    reservation.window_start,
    reservation.state,
    reservation.reserved_input_tokens,
    reservation.reserved_output_tokens,
    reservation.reserved_cost_usd,
    reservation.actual_input_tokens,
    reservation.actual_output_tokens,
    reservation.actual_cost_usd
  FROM provider_call_reservations AS reservation
  CROSS JOIN LATERAL (
    VALUES
      (reservation.global_scope_id),
      (reservation.provider_scope_id),
      (reservation.model_scope_id),
      (reservation.run_scope_id)
  ) AS scope_ids(scope_id)
  WHERE scope_ids.scope_id IS NOT NULL
),
derived_scopes AS MATERIALIZED (
  SELECT
    scope_id,
    count(*) FILTER (WHERE state IN ('reserved', 'send_started'))
      AS active_reservations,
    count(*) FILTER (WHERE state = 'reserved') AS reserved_requests,
    count(*) FILTER (
      WHERE state IN ('send_started', 'settled_actual', 'settled_conservative')
    ) AS consumed_requests,
    COALESCE(sum(reserved_input_tokens) FILTER (
      WHERE state IN ('reserved', 'send_started')
    ), 0) AS reserved_input_tokens,
    COALESCE(sum(reserved_output_tokens) FILTER (
      WHERE state IN ('reserved', 'send_started')
    ), 0) AS reserved_output_tokens,
    COALESCE(sum(reserved_cost_usd) FILTER (
      WHERE state IN ('reserved', 'send_started')
    ), 0) AS reserved_cost_usd,
    COALESCE(sum(actual_input_tokens) FILTER (
      WHERE state IN ('settled_actual', 'settled_conservative')
    ), 0) AS consumed_input_tokens,
    COALESCE(sum(actual_output_tokens) FILTER (
      WHERE state IN ('settled_actual', 'settled_conservative')
    ), 0) AS consumed_output_tokens,
    COALESCE(sum(actual_cost_usd) FILTER (
      WHERE state IN ('settled_actual', 'settled_conservative')
    ), 0) AS consumed_cost_usd,
    COALESCE(bool_or(
      state IN ('settled_actual', 'settled_conservative')
      AND (
        (
          reserved_input_tokens IS NOT NULL
          AND actual_input_tokens IS NOT NULL
          AND actual_input_tokens > reserved_input_tokens
        )
        OR (
          reserved_output_tokens IS NOT NULL
          AND actual_output_tokens IS NOT NULL
          AND actual_output_tokens > reserved_output_tokens
        )
        OR (
          reserved_cost_usd IS NOT NULL
          AND actual_cost_usd IS NOT NULL
          AND actual_cost_usd > reserved_cost_usd
        )
      )
    ), false) AS overdrawn
  FROM scope_facts
  GROUP BY scope_id
),
derived_minutes AS MATERIALIZED (
  SELECT
    scope_id,
    policy_id,
    window_start,
    count(*) FILTER (WHERE state = 'reserved') AS reserved_requests,
    count(*) FILTER (
      WHERE state IN ('send_started', 'settled_actual', 'settled_conservative')
    ) AS consumed_requests,
    COALESCE(sum(reserved_input_tokens) FILTER (
      WHERE state IN ('reserved', 'send_started')
    ), 0) AS reserved_input_tokens,
    COALESCE(sum(reserved_output_tokens) FILTER (
      WHERE state IN ('reserved', 'send_started')
    ), 0) AS reserved_output_tokens,
    COALESCE(sum(actual_input_tokens) FILTER (
      WHERE state IN ('settled_actual', 'settled_conservative')
    ), 0) AS consumed_input_tokens,
    COALESCE(sum(actual_output_tokens) FILTER (
      WHERE state IN ('settled_actual', 'settled_conservative')
    ), 0) AS consumed_output_tokens
  FROM scope_facts
  GROUP BY scope_id, policy_id, window_start
)
SELECT json_build_object(
  'policies', (SELECT count(*) FROM governance_policies),
  'active_policies', (SELECT count(*) FROM governance_policies WHERE is_active),
  'runs', (SELECT count(*) FROM evaluation_runs),
  'responses', (SELECT count(*) FROM evaluation_responses),
  'distinct_run_question_responses', (
    SELECT count(*) FROM (
      SELECT run_id, question_id FROM evaluation_responses GROUP BY run_id, question_id
    ) distinct_responses
  ),
  'duplicate_response_questions', (
    SELECT count(*) FROM (
      SELECT run_id, question_id FROM evaluation_responses
      GROUP BY run_id, question_id HAVING count(*) > 1
    ) duplicates
  ),
  'question_executions', (SELECT count(*) FROM question_executions),
  'reservations', (SELECT count(*) FROM provider_call_reservations),
  'reservation_states', COALESCE((
    SELECT json_object_agg(state, count) FROM (
      SELECT state, count(*) AS count
      FROM provider_call_reservations GROUP BY state ORDER BY state
    ) states
  ), '{}'::json),
  'active_reservations', (
    SELECT count(*) FROM provider_call_reservations
    WHERE state IN ('reserved', 'send_started')
  ),
  'scope_active_reservations', (
    SELECT COALESCE(sum(active_reservations), 0) FROM governance_scopes
  ),
  'scope_reserved_requests', (
    SELECT COALESCE(sum(reserved_requests), 0) FROM governance_scopes
  ),
  'scope_reserved_input_tokens', (
    SELECT COALESCE(sum(reserved_input_tokens), 0) FROM governance_scopes
  ),
  'scope_reserved_output_tokens', (
    SELECT COALESCE(sum(reserved_output_tokens), 0) FROM governance_scopes
  ),
  'minute_reserved_requests', (
    SELECT COALESCE(sum(reserved_requests), 0) FROM governance_minute_buckets
  ),
  'minute_reserved_input_tokens', (
    SELECT COALESCE(sum(reserved_input_tokens), 0) FROM governance_minute_buckets
  ),
  'minute_reserved_output_tokens', (
    SELECT COALESCE(sum(reserved_output_tokens), 0) FROM governance_minute_buckets
  ),
  'overdrawn_scopes', (SELECT count(*) FROM governance_scopes WHERE overdrawn),
  'missing_scope_projection_rows', (
    SELECT count(*) FROM derived_scopes AS derived
    LEFT JOIN governance_scopes AS materialized ON materialized.id = derived.scope_id
    WHERE materialized.id IS NULL
  ),
  'extra_scope_projection_rows', (
    SELECT count(*) FROM governance_scopes AS materialized
    LEFT JOIN derived_scopes AS derived ON derived.scope_id = materialized.id
    WHERE derived.scope_id IS NULL
  ),
  'scope_projection_field_drift', (
    SELECT count(*) FROM governance_scopes AS materialized
    JOIN derived_scopes AS derived ON derived.scope_id = materialized.id
    WHERE
      materialized.active_reservations IS DISTINCT FROM derived.active_reservations
      OR materialized.reserved_requests IS DISTINCT FROM derived.reserved_requests
      OR materialized.consumed_requests IS DISTINCT FROM derived.consumed_requests
      OR materialized.reserved_input_tokens IS DISTINCT FROM derived.reserved_input_tokens
      OR materialized.reserved_output_tokens IS DISTINCT FROM derived.reserved_output_tokens
      OR materialized.reserved_cost_usd IS DISTINCT FROM derived.reserved_cost_usd
      OR materialized.consumed_input_tokens IS DISTINCT FROM derived.consumed_input_tokens
      OR materialized.consumed_output_tokens IS DISTINCT FROM derived.consumed_output_tokens
      OR materialized.consumed_cost_usd IS DISTINCT FROM derived.consumed_cost_usd
      OR materialized.overdrawn IS DISTINCT FROM derived.overdrawn
  ),
  'missing_minute_projection_rows', (
    SELECT count(*) FROM derived_minutes AS derived
    LEFT JOIN governance_minute_buckets AS materialized
      ON materialized.scope_id = derived.scope_id
      AND materialized.policy_id = derived.policy_id
      AND materialized.window_start = derived.window_start
    WHERE materialized.id IS NULL
  ),
  'extra_minute_projection_rows', (
    SELECT count(*) FROM governance_minute_buckets AS materialized
    LEFT JOIN derived_minutes AS derived
      ON derived.scope_id = materialized.scope_id
      AND derived.policy_id = materialized.policy_id
      AND derived.window_start = materialized.window_start
    WHERE derived.scope_id IS NULL
  ),
  'minute_projection_field_drift', (
    SELECT count(*) FROM governance_minute_buckets AS materialized
    JOIN derived_minutes AS derived
      ON derived.scope_id = materialized.scope_id
      AND derived.policy_id = materialized.policy_id
      AND derived.window_start = materialized.window_start
    WHERE
      materialized.reserved_requests IS DISTINCT FROM derived.reserved_requests
      OR materialized.consumed_requests IS DISTINCT FROM derived.consumed_requests
      OR materialized.reserved_input_tokens IS DISTINCT FROM derived.reserved_input_tokens
      OR materialized.reserved_output_tokens IS DISTINCT FROM derived.reserved_output_tokens
      OR materialized.consumed_input_tokens IS DISTINCT FROM derived.consumed_input_tokens
      OR materialized.consumed_output_tokens IS DISTINCT FROM derived.consumed_output_tokens
  ),
  'duplicate_operation_keys', (
    SELECT count(*) FROM (
      SELECT operation_key FROM provider_call_reservations
      GROUP BY operation_key HAVING count(*) > 1
    ) duplicates
  ),
  'audit_events', (SELECT count(*) FROM audit_events),
  'audit_event_types', COALESCE((
    SELECT json_object_agg(event_type, count) FROM (
      SELECT event_type, count(*) AS count
      FROM audit_events GROUP BY event_type ORDER BY event_type
    ) types
  ), '{}'::json),
  'duplicate_audit_event_keys', (
    SELECT count(*) FROM (
      SELECT event_key FROM audit_events GROUP BY event_key HAVING count(*) > 1
    ) duplicates
  ),
  'active_runs', (
    SELECT count(*) FROM evaluation_runs WHERE status IN ('pending', 'running')
  ),
  'failed_attempt_count', (
    SELECT COALESCE(sum(failed_attempt_count), 0) FROM evaluation_runs
  ),
  'question_error_count', (
    SELECT count(*) FROM evaluation_responses WHERE error_type IS NOT NULL
  )
)::text;
"""


def _reconciliation_integer(
    snapshot: dict[str, Any],
    field: str,
    *,
    context: str = "reconciliation",
) -> int:
    value = snapshot.get(field)
    if type(value) is not int:
        raise AcceptanceFailure(f"{context}.{field} must be an integer")
    return value


def validate_governance_reconciliation_snapshot(
    snapshot: dict[str, Any],
    *,
    expected_counts: dict[str, int] | None = None,
    expected_reservation_states: dict[str, int] | None = None,
    expected_provider_attempt_audit_counts: dict[str, int] | None = None,
) -> None:
    """Enforce the configured workload's ledger and projection invariants."""

    expected_counts = expected_counts or EXPECTED_RECONCILIATION_COUNTS
    expected_reservation_states = expected_reservation_states or EXPECTED_RESERVATION_STATES
    expected_provider_attempt_audit_counts = (
        expected_provider_attempt_audit_counts or EXPECTED_PROVIDER_ATTEMPT_AUDIT_COUNTS
    )

    for field in RECONCILIATION_ZERO_FIELDS:
        value = _reconciliation_integer(snapshot, field)
        if value != 0:
            raise AcceptanceFailure(f"governance reconciliation drift: {field}={value}")

    for field, expected in expected_counts.items():
        value = _reconciliation_integer(snapshot, field)
        if value != expected:
            raise AcceptanceFailure(
                f"governance reconciliation count drift: {field}={value}, expected={expected}"
            )

    if snapshot["distinct_run_question_responses"] != snapshot["responses"]:
        raise AcceptanceFailure("Response run/question cardinality drift")

    reservation_states = snapshot.get("reservation_states")
    if not isinstance(reservation_states, dict):
        raise AcceptanceFailure("reconciliation.reservation_states must be an object")
    for state in reservation_states:
        _reconciliation_integer(
            reservation_states,
            state,
            context="reconciliation.reservation_states",
        )
    if reservation_states != expected_reservation_states:
        raise AcceptanceFailure("Provider reservation terminal-state counts drift")

    audit_event_types = snapshot.get("audit_event_types")
    if not isinstance(audit_event_types, dict):
        raise AcceptanceFailure("reconciliation.audit_event_types must be an object")
    for event_type, expected in expected_provider_attempt_audit_counts.items():
        observed = _reconciliation_integer(
            audit_event_types,
            event_type,
            context="reconciliation.audit_event_types",
        )
        if observed != expected:
            raise AcceptanceFailure(
                f"Provider attempt audit count drift: {event_type}={observed}, expected={expected}"
            )

    if _reconciliation_integer(snapshot, "audit_events") <= 0:
        raise AcceptanceFailure("capacity evidence did not produce typed audit events")


def finite_capacity_policy(
    *,
    backlog_limit: int,
    question_quantum: int,
) -> dict[str, int | str]:
    """Return the explicit finite policy used by every capacity scenario."""

    return {
        "global_concurrency_limit": 32,
        "provider_concurrency_limit": 32,
        "model_concurrency_limit": 16,
        "run_concurrency_limit": 4,
        "global_requests_per_minute": 100_000,
        "provider_requests_per_minute": 100_000,
        "model_requests_per_minute": 50_000,
        "run_requests_per_minute": 1_000,
        "global_tokens_per_minute": 100_000_000,
        "provider_tokens_per_minute": 100_000_000,
        "model_tokens_per_minute": 50_000_000,
        "run_tokens_per_minute": 1_000_000,
        "global_lifetime_request_budget": 100_000,
        "global_lifetime_token_budget": 100_000_000,
        "global_lifetime_cost_budget_usd": "1000.00000000",
        "run_lifetime_request_budget": RUN_LIFETIME_REQUEST_BUDGET,
        "run_lifetime_token_budget": RUN_LIFETIME_TOKEN_BUDGET,
        "run_lifetime_cost_budget_usd": RUN_LIFETIME_COST_BUDGET_USD,
        "backlog_limit": backlog_limit,
        "question_quantum": question_quantum,
    }


def summarize_submissions(
    submissions: Sequence[dict[str, Any]],
    *,
    duration_seconds: float,
) -> dict[str, Any]:
    """Preserve accepted payloads and exact status-bearing rejection evidence."""

    status_counts: dict[str, int] = {}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for submission in submissions:
        status_code = int(submission["status_code"])
        status_key = str(status_code)
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
        payload = dict(submission["payload"])
        if status_code == 202:
            accepted.append(payload)
        else:
            rejected.append({"status_code": status_code, "payload": payload})
    return {
        "requested": len(submissions),
        "accepted": accepted,
        "rejected": rejected,
        "status_counts": status_counts,
        "duration_seconds": round(duration_seconds, 6),
        "request_latency_seconds": distribution(
            [float(item["elapsed_seconds"]) for item in submissions]
        ),
    }


def cooperative_scheduling_summary(
    final_runs: Sequence[dict[str, Any]],
    audit_events: dict[str, Sequence[dict[str, Any]]],
) -> dict[str, Any]:
    """Summarize the durable claim/yield proof for bounded Run slices."""

    per_run: list[dict[str, Any]] = []
    for run in final_runs:
        run_id = str(run["id"])
        events = audit_events.get(run_id, ())
        per_run.append(
            {
                "run_id": run_id,
                "dispatch_count": int(run.get("dispatch_count") or 0),
                "claim_events": sum(event.get("event_type") == "run_claimed" for event in events),
                "cooperative_yield_events": sum(
                    event.get("event_type") == "run_yielded" for event in events
                ),
            }
        )
    return {
        "all_runs_dispatched_more_than_once": bool(per_run)
        and all(item["dispatch_count"] > 1 for item in per_run),
        "all_runs_yielded": bool(per_run)
        and all(item["cooperative_yield_events"] > 0 for item in per_run),
        "claim_events": sum(item["claim_events"] for item in per_run),
        "cooperative_yield_events": sum(item["cooperative_yield_events"] for item in per_run),
        "per_run": per_run,
    }


def fairness_ordering_summary(
    *,
    high_run_ids: Sequence[str],
    low_run_id: str,
    audit_events: dict[str, Sequence[dict[str, Any]]],
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Build ordered evidence that a low-volume Model received an early slice."""

    high_ids = set(high_run_ids)
    ordered: list[dict[str, Any]] = []
    for run_id in (*high_run_ids, low_run_id):
        for event in audit_events.get(run_id, ()):
            event_type = str(event.get("event_type") or "")
            if event_type not in {"run_claimed", "run_yielded", "run_terminal"}:
                continue
            ordered.append(
                {
                    "event_id": str(event["id"]),
                    "event_type": event_type,
                    "occurred_at": str(event["occurred_at"]),
                    "run_id": run_id,
                    "role": "low_volume" if run_id == low_run_id else "high_volume",
                }
            )
    ordered.sort(key=lambda event: (parse_datetime(event["occurred_at"]), event["event_id"]))
    low_claim_times = [
        parse_datetime(event["occurred_at"])
        for event in ordered
        if event["run_id"] == low_run_id and event["event_type"] == "run_claimed"
    ]
    high_terminal_times = [
        parse_datetime(event["occurred_at"])
        for event in ordered
        if event["run_id"] in high_ids and event["event_type"] == "run_terminal"
    ]
    low_observed = dict(observation["low_run"])
    high_observed = [dict(run) for run in observation["high_runs"]]
    return {
        "low_volume_claim_observed": bool(low_claim_times),
        "low_volume_slice_observed": int(low_observed.get("completed_questions") or 0) > 0,
        "high_volume_incomplete_at_low_slice": sum(
            run.get("status") not in TERMINAL_STATUSES for run in high_observed
        ),
        "low_claim_before_high_backlog_drained": bool(low_claim_times)
        and bool(high_terminal_times)
        and min(low_claim_times) < max(high_terminal_times),
        "observation": {
            "low_run": low_observed,
            "high_runs": high_observed,
        },
        "ordered_events": ordered,
    }


def percentile(values: Sequence[float], percentage: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""

    if not values:
        raise ValueError("percentile requires at least one sample")
    if not 0 <= percentage <= 100:
        raise ValueError("percentage must be between 0 and 100")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentage / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def distribution(values: Sequence[float]) -> dict[str, int | float | None]:
    """Summarize a latency or count sample without retaining every observation."""

    if not values:
        return {
            "count": 0,
            "min": None,
            "mean": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }
    numeric = [float(value) for value in values]
    return {
        "count": len(numeric),
        "min": round(min(numeric), 6),
        "mean": round(statistics.fmean(numeric), 6),
        "p50": round(percentile(numeric, 50), 6),
        "p95": round(percentile(numeric, 95), 6),
        "p99": round(percentile(numeric, 99), 6),
        "max": round(max(numeric), 6),
    }


def nonnegative_utc_elapsed_seconds(started_at: str, finished_at: str) -> float:
    """Return a finite non-negative duration derived from two UTC facts."""

    seconds = (parse_datetime(finished_at) - parse_datetime(started_at)).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("UTC duration must be finite and non-negative")
    return round(seconds, 6)


def first_claim_at_or_after(
    audit_events: Sequence[dict[str, Any]],
    not_before: str,
) -> dict[str, Any]:
    """Return the first typed claim whose durable occurrence is not before a DB fact."""

    threshold = parse_datetime(not_before)
    candidates = [
        event
        for event in audit_events
        if event.get("event_type") == "run_claimed"
        and isinstance(event.get("occurred_at"), str)
        and parse_datetime(str(event["occurred_at"])) >= threshold
    ]
    if not candidates:
        raise ValueError("no typed run_claimed event occurred at or after the threshold")
    return min(
        candidates,
        key=lambda event: (parse_datetime(str(event["occurred_at"])), str(event.get("id") or "")),
    )


def _validated_worker_owner(
    worker_id: object,
    validated_workers: dict[str, dict[str, Any]],
) -> str:
    """Parse an exact production Worker owner and map it to inspected runtime facts."""

    if not isinstance(worker_id, str):
        raise AcceptanceFailure("burst claim omitted a typed Worker owner")
    parts = worker_id.split(":")
    if len(parts) != 4 or parts[0] != "worker":
        raise AcceptanceFailure("burst claim Worker owner did not use the exact runtime format")
    hostname, pid_text, instance_text = parts[1:]
    try:
        pid = int(pid_text)
        instance = uuid.UUID(instance_text)
    except (ValueError, AttributeError) as exc:
        raise AcceptanceFailure("burst claim Worker owner was malformed") from exc
    if (
        pid <= 0
        or pid > 2_147_483_647
        or pid_text != str(pid)
        or instance.version != 4
        or str(instance) != instance_text
    ):
        raise AcceptanceFailure("burst claim Worker owner was not canonical")
    if hostname not in validated_workers:
        raise AcceptanceFailure("burst claim owner did not map to a validated Worker")
    return hostname


def burst_worker_participation(
    *,
    accepted_run_ids: Sequence[str],
    audit_events: dict[str, Sequence[dict[str, Any]]],
    worker_state: Sequence[dict[str, Any]],
    project: str,
    backlog_ready_at: str,
) -> dict[str, Any]:
    """Return raw, independently checkable claim ownership for one formal burst."""

    accepted = set(accepted_run_ids)
    if len(accepted) != len(accepted_run_ids) or set(audit_events) != accepted:
        raise AcceptanceFailure("burst claim evidence did not match the accepted Run set")
    if len(worker_state) != 2:
        raise AcceptanceFailure("formal burst did not validate exactly two Workers")

    validated: dict[str, dict[str, Any]] = {}
    for worker in worker_state:
        hostname = worker.get("hostname")
        container_id = worker.get("id")
        if (
            not isinstance(hostname, str)
            or not hostname
            or ":" in hostname
            or any(character.isspace() or ord(character) < 32 for character in hostname)
            or not _is_container_identity(container_id)
            or worker.get("project") != project
            or worker.get("service") != "worker"
            or worker.get("status") != "running"
            or worker.get("health") != "healthy"
        ):
            raise AcceptanceFailure("formal burst Worker runtime metadata was invalid")
        if hostname in validated:
            raise AcceptanceFailure("formal burst Worker runtime identities were not unique")
        validated[hostname] = {
            "container_id": container_id,
            "hostname": hostname,
        }

    threshold = parse_datetime(backlog_ready_at)
    claims: list[dict[str, str]] = []
    owner_ids: set[str] = set()
    mapped_workers: set[str] = set()
    for run_id in accepted_run_ids:
        for event in audit_events[run_id]:
            if event.get("event_type") != "run_claimed":
                continue
            occurred_at = event.get("occurred_at")
            if not isinstance(occurred_at, str) or parse_datetime(occurred_at) < threshold:
                continue
            worker_id = event.get("worker_id")
            mapped_hostname = _validated_worker_owner(worker_id, validated)
            owner_ids.add(str(worker_id))
            mapped_workers.add(mapped_hostname)
            claims.append(
                {
                    "run_id": run_id,
                    "worker_id": str(worker_id),
                    "occurred_at": occurred_at,
                }
            )

    claims.sort(key=lambda item: (parse_datetime(item["occurred_at"]), item["run_id"]))
    if {claim["run_id"] for claim in claims} != accepted:
        raise AcceptanceFailure("formal burst did not retain a boundary claim for every Run")
    if len(owner_ids) != 2 or len(mapped_workers) != 2:
        raise AcceptanceFailure("formal burst did not prove exactly two distinct claim Workers")
    return {
        "validated_workers": sorted(validated.values(), key=lambda item: item["container_id"]),
        "claims": claims,
        "distinct_claim_workers": len(owner_ids),
        "all_claim_workers_validated": True,
    }


def formal_worker_state_witness(
    worker_state: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project validated formal Worker metadata onto the consumer contract."""

    fields = ("id", "hostname", "project", "service", "status", "health")
    return [{field: worker[field] for field in fields} for worker in worker_state]


def burst_segmented_timing(
    *,
    final_runs: Sequence[dict[str, Any]],
    audit_events: dict[str, Sequence[dict[str, Any]]],
    participation: dict[str, Any],
    suspend_completed_at: str,
    backlog_ready_at: str,
    restore_completed_at: str,
    suspend_seconds: float,
    backlog_build_seconds: float,
    restore_command_seconds: float,
    drain_seconds: float,
) -> dict[str, Any]:
    """Keep UTC durable facts and process-monotonic durations in separate domains."""

    claims = list(participation["claims"])
    first_claim = min(claims, key=lambda item: parse_datetime(item["occurred_at"]))
    first_by_owner: dict[str, dict[str, str]] = {}
    for claim in claims:
        first_by_owner.setdefault(claim["worker_id"], claim)
    all_workers_first_claim = max(
        first_by_owner.values(), key=lambda item: parse_datetime(item["occurred_at"])
    )

    accepted = {str(run["id"]) for run in final_runs}
    threshold = parse_datetime(backlog_ready_at)
    claim_or_yield: list[dict[str, str]] = []
    for run_id in sorted(accepted):
        for event in audit_events[run_id]:
            event_type = event.get("event_type")
            occurred_at = event.get("occurred_at")
            if (
                event_type in {"run_claimed", "run_yielded"}
                and isinstance(occurred_at, str)
                and parse_datetime(occurred_at) >= threshold
            ):
                claim_or_yield.append(
                    {
                        "run_id": run_id,
                        "event_type": str(event_type),
                        "occurred_at": occurred_at,
                    }
                )
    claim_or_yield.sort(
        key=lambda item: (parse_datetime(item["occurred_at"]), item["run_id"], item["event_type"])
    )
    adjacent_gaps = [
        nonnegative_utc_elapsed_seconds(previous["occurred_at"], current["occurred_at"])
        for previous, current in pairwise(claim_or_yield)
    ]

    claims_by_run: dict[str, list[dict[str, str]]] = {run_id: [] for run_id in accepted}
    for claim in claims:
        claims_by_run[claim["run_id"]].append(claim)
    run_timings: list[dict[str, Any]] = []
    for run in sorted(final_runs, key=lambda item: str(item["id"])):
        run_id = str(run["id"])
        finished_at = run.get("finished_at")
        if not isinstance(finished_at, str) or not claims_by_run[run_id]:
            raise AcceptanceFailure("formal burst Run timing facts were incomplete")
        run_first_claim = min(
            claims_by_run[run_id], key=lambda item: parse_datetime(item["occurred_at"])
        )["occurred_at"]
        run_timings.append(
            {
                "run_id": run_id,
                "first_claim_at": run_first_claim,
                "finished_at": finished_at,
                "duration_seconds": nonnegative_utc_elapsed_seconds(run_first_claim, finished_at),
            }
        )

    first_claim_to_finish = [float(item["duration_seconds"]) for item in run_timings]
    return {
        "clock_domains": {
            "monotonic_seconds": "process_monotonic",
            "durable_utc": "database_utc",
        },
        "monotonic_seconds": {
            "suspend": round(suspend_seconds, 6),
            "backlog_build": round(backlog_build_seconds, 6),
            "restore_command": round(restore_command_seconds, 6),
            "drain": round(drain_seconds, 6),
        },
        "durable_utc": {
            "suspend_completed_at": suspend_completed_at,
            "backlog_ready_at": backlog_ready_at,
            "restore_completed_at": restore_completed_at,
            "first_claim_at": first_claim["occurred_at"],
            "all_workers_first_claim_at": all_workers_first_claim["occurred_at"],
            "claim_or_yield_events": claim_or_yield,
            "run_first_claim_to_finish": run_timings,
        },
        "durable_seconds": {
            "backlog_ready_to_first_claim": nonnegative_utc_elapsed_seconds(
                backlog_ready_at, first_claim["occurred_at"]
            ),
            "backlog_ready_to_all_workers_first_claim": nonnegative_utc_elapsed_seconds(
                backlog_ready_at, all_workers_first_claim["occurred_at"]
            ),
            "adjacent_claim_or_yield_gap": {
                **distribution(adjacent_gaps),
                "samples": adjacent_gaps,
            },
            "first_claim_to_finish": {
                **distribution(first_claim_to_finish),
                "samples": first_claim_to_finish,
            },
        },
    }


def validate_arguments(args: argparse.Namespace) -> None:
    if args.workers < 2:
        raise ValueError("--workers must be at least 2")
    if not 1 <= args.runs_per_phase <= 100:
        raise ValueError("--runs-per-phase must be between 1 and 100")
    if not 3 <= args.backlog_limit <= 100:
        raise ValueError("--backlog-limit must be between 3 and 100")
    if args.runs_per_phase > args.backlog_limit:
        raise ValueError("--runs-per-phase must not exceed --backlog-limit")
    if not 1 <= args.burst_runs <= 100:
        raise ValueError("--burst-runs must be between 1 and 100")
    if args.burst_runs <= args.backlog_limit:
        raise ValueError("--burst-runs must be strictly greater than --backlog-limit")
    if not 2 <= args.submit_concurrency <= 32:
        raise ValueError("--submit-concurrency must be between 2 and 32")
    if not 1 <= args.run_concurrency <= 4:
        raise ValueError("--run-concurrency must be between 1 and 4")
    if not 1 <= args.question_quantum < DEMO_QUESTIONS_PER_RUN:
        raise ValueError("--question-quantum must be less than the 15-question demo Run")
    if not 0.01 <= args.mock_delay_seconds <= 10:
        raise ValueError("--mock-delay-seconds must be between 0.01 and 10")
    if not 30 <= args.timeout_seconds <= 3600:
        raise ValueError("--timeout-seconds must be between 30 and 3600")
    if not 3 <= args.lease_seconds <= 3600:
        raise ValueError("--lease-seconds must be between 3 and 3600")
    if not 1 <= args.heartbeat_seconds <= 1200:
        raise ValueError("--heartbeat-seconds must be between 1 and 1200")
    if args.heartbeat_seconds * 2 > args.lease_seconds:
        raise ValueError("--heartbeat-seconds must be at most half --lease-seconds")
    if not 0.05 <= args.worker_poll_seconds <= 60:
        raise ValueError("--worker-poll-seconds must be between 0.05 and 60")
    if not 1 <= args.worker_max_attempts <= 20:
        raise ValueError("--worker-max-attempts must be between 1 and 20")
    if not 0 <= args.retry_backoff_base_seconds <= 3600:
        raise ValueError("--retry-backoff-base-seconds must be between 0 and 3600")
    if not args.retry_backoff_base_seconds <= args.retry_backoff_cap_seconds <= 86_400:
        raise ValueError("--retry-backoff-cap-seconds must be between the base delay and 86400")
    if not 50 <= args.redis_block_milliseconds <= 60_000:
        raise ValueError("--redis-block-milliseconds must be between 50 and 60000")
    if not 0 < args.redis_operation_timeout_seconds <= 30:
        raise ValueError("--redis-operation-timeout-seconds must be between 0 and 30")
    if not 0 <= args.worker_shutdown_grace_seconds <= 3600:
        raise ValueError("--worker-shutdown-grace-seconds must be between 0 and 3600")
    if args.qualification_profile == FORMAL_QUALIFICATION_PROFILE:
        differing_fields = [
            field
            for field, expected in FORMAL_V2_ARGUMENTS.items()
            if getattr(args, field) != expected
        ]
        if differing_fields:
            raise ValueError(
                "P2-local-control-plane-v2 fixed configuration drift: "
                + json.dumps(
                    {"differing_fields": differing_fields},
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )


class Phase2Capacity(Phase2Acceptance):
    """Own and measure one isolated real-Compose capacity project."""

    def __init__(self, repository_root: Path, args: argparse.Namespace) -> None:
        super().__init__(repository_root, args.artifacts_root)
        self.worker_count = int(args.workers)
        self.runs_per_phase = int(args.runs_per_phase)
        self.backlog_limit = int(args.backlog_limit)
        self.burst_runs = int(args.burst_runs)
        self.submit_concurrency = int(args.submit_concurrency)
        self.run_concurrency = int(args.run_concurrency)
        self.question_quantum = int(args.question_quantum)
        self.mock_delay_seconds = float(args.mock_delay_seconds)
        self.timeout_seconds = float(args.timeout_seconds)
        self.lease_seconds = float(args.lease_seconds)
        self.heartbeat_seconds = float(args.heartbeat_seconds)
        self.worker_poll_seconds = float(args.worker_poll_seconds)
        self.worker_max_attempts = int(args.worker_max_attempts)
        self.retry_backoff_base_seconds = float(args.retry_backoff_base_seconds)
        self.retry_backoff_cap_seconds = float(args.retry_backoff_cap_seconds)
        self.redis_block_milliseconds = int(args.redis_block_milliseconds)
        self.redis_operation_timeout_seconds = float(args.redis_operation_timeout_seconds)
        self.worker_shutdown_grace_seconds = float(args.worker_shutdown_grace_seconds)
        self.measurement_order = str(args.measurement_order)
        self.qualification_profile = str(args.qualification_profile)
        self.formal_slo_v2 = self.qualification_profile == FORMAL_QUALIFICATION_PROFILE
        self.low_volume_model_id: str | None = None
        self.policy_document: dict[str, Any] | None = None
        self.env["LLMBENCHLAB_COMPOSE_MOCK_GENERATION_DELAY_SECONDS"] = str(self.mock_delay_seconds)
        self.env["LLMBENCHLAB_COMPOSE_WORKER_LEASE_SECONDS"] = str(self.lease_seconds)
        self.env["LLMBENCHLAB_COMPOSE_WORKER_HEARTBEAT_SECONDS"] = str(self.heartbeat_seconds)
        self.env["LLMBENCHLAB_COMPOSE_WORKER_POLL_SECONDS"] = str(self.worker_poll_seconds)
        self.env["LLMBENCHLAB_COMPOSE_WORKER_MAX_ATTEMPTS"] = str(self.worker_max_attempts)
        self.env["LLMBENCHLAB_COMPOSE_WORKER_RETRY_BACKOFF_BASE_SECONDS"] = str(
            self.retry_backoff_base_seconds
        )
        self.env["LLMBENCHLAB_COMPOSE_WORKER_RETRY_BACKOFF_CAP_SECONDS"] = str(
            self.retry_backoff_cap_seconds
        )
        self.env["LLMBENCHLAB_COMPOSE_WORKER_SHUTDOWN_GRACE_SECONDS"] = str(
            self.worker_shutdown_grace_seconds
        )
        self.env["LLMBENCHLAB_COMPOSE_REDIS_BLOCK_MILLISECONDS"] = str(
            self.redis_block_milliseconds
        )
        self.env["LLMBENCHLAB_COMPOSE_REDIS_OPERATION_TIMEOUT_SECONDS"] = str(
            self.redis_operation_timeout_seconds
        )
        self.env["LLMBENCHLAB_COMPOSE_DATABASE_POOL_SIZE"] = str(DATABASE_POOL_SIZE)
        self.env["LLMBENCHLAB_COMPOSE_DATABASE_MAX_OVERFLOW"] = str(DATABASE_MAX_OVERFLOW)
        self.env["LLMBENCHLAB_COMPOSE_DATABASE_POOL_TIMEOUT_SECONDS"] = str(
            DATABASE_POOL_TIMEOUT_SECONDS
        )
        self.env["LLMBENCHLAB_COMPOSE_READINESS_DATABASE_TIMEOUT_SECONDS"] = str(
            READINESS_DATABASE_TIMEOUT_SECONDS
        )
        for inherited_key in PROVIDER_CREDENTIAL_ENV_KEYS:
            self.env.pop(inherited_key, None)

        self.artifact_dir = (
            args.artifacts_root
            if args.artifacts_root.is_absolute()
            else self.root / args.artifacts_root
        ).resolve() / self.project
        self.evidence_path = self.artifact_dir / "evidence.json"
        self.evidence = {
            "schema_version": (
                CAPACITY_EVIDENCE_SCHEMA_V2 if self.formal_slo_v2 else CAPACITY_EVIDENCE_SCHEMA_V1
            ),
            "status": "initializing",
            "started_at": utc_now(),
            "finished_at": None,
            "project_name": self.project,
            "artifacts": str(self.evidence_path.relative_to(self.root)),
            "offline_only": True,
            "production_slo": False,
            "admission_scope": (
                "Run admission is a database-serialized local backlog decision; it is not "
                "Provider-side admission, billing evidence, or a Provider SLA."
            ),
            "support_boundary": (
                "Results apply only to this commit, host, container limits, Mock data, "
                "and recorded configuration; they do not establish a production SLO/SLA, "
                "real-Provider capacity, HA, or unlimited scaling."
            ),
            "configuration": {
                "qualification_profile": self.qualification_profile,
                "workers": self.worker_count,
                "runs_per_measurement_phase": self.runs_per_phase,
                "backlog_limit": self.backlog_limit,
                "concurrent_backlog_submissions": self.burst_runs,
                "submit_concurrency": self.submit_concurrency,
                "run_concurrency": self.run_concurrency,
                "question_quantum": self.question_quantum,
                "questions_per_run": DEMO_QUESTIONS_PER_RUN,
                "run_max_tokens": RUN_MAX_TOKENS,
                "run_input_token_reservation": RUN_INPUT_TOKEN_RESERVATION,
                "mock_generation_delay_seconds": self.mock_delay_seconds,
                "timeout_seconds": self.timeout_seconds,
                "lease_seconds": self.lease_seconds,
                "heartbeat_seconds": self.heartbeat_seconds,
                "worker_poll_seconds": self.worker_poll_seconds,
                "worker_max_attempts": self.worker_max_attempts,
                "retry_backoff_base_seconds": self.retry_backoff_base_seconds,
                "retry_backoff_cap_seconds": self.retry_backoff_cap_seconds,
                "worker_shutdown_grace_seconds": self.worker_shutdown_grace_seconds,
                "redis_block_milliseconds": self.redis_block_milliseconds,
                "redis_operation_timeout_seconds": self.redis_operation_timeout_seconds,
                "measurement_order": self.measurement_order,
                "database_pool_size": DATABASE_POOL_SIZE,
                "database_max_overflow": DATABASE_MAX_OVERFLOW,
                "database_pool_timeout_seconds": DATABASE_POOL_TIMEOUT_SECONDS,
                "readiness_database_timeout_seconds": READINESS_DATABASE_TIMEOUT_SECONDS,
            },
            "environment": {},
            "data": {},
            "topology": {},
            "measurements": [],
            "fairness": {},
            "faults": [],
            "reconciliation": {},
            "commands": [],
            "self_review": {},
            "diagnostics": {},
            "cleanup": {},
            "failure": None,
        }

    def self_review(self) -> dict[str, Any]:
        review = super().self_review()
        self.evidence["repository"]["capacity_script_sha256"] = hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest()
        review.update(
            {
                "capacity_workers_at_least_two": self.worker_count >= 2,
                "mock_only": True,
                "finite_governance_policy": all(
                    value is not None
                    for value in finite_capacity_policy(
                        backlog_limit=self.backlog_limit,
                        question_quantum=self.question_quantum,
                    ).values()
                ),
                "cooperative_question_quantum": (
                    1 <= self.question_quantum < DEMO_QUESTIONS_PER_RUN
                ),
                "local_admission_not_provider_sla": True,
                "capacity_evidence_schema": self.evidence.get("schema_version", EVIDENCE_SCHEMA),
                "real_provider_credentials_removed": list(PROVIDER_CREDENTIAL_ENV_KEYS),
            }
        )
        return review

    def setup_stack(self) -> None:
        self.stack_touched = True
        self.compose(
            "up",
            "--build",
            "-d",
            "--wait",
            "--scale",
            f"worker={self.worker_count}",
            "postgres",
            "redis",
            "migrate",
            "api",
            "worker",
            timeout=600,
            max_recorded_chars=24000,
        )

    def scale_workers(self, count: int) -> list[dict[str, Any]]:
        self.require(count >= 1, "capacity phases require at least one running Worker")
        self.compose(
            "up",
            "-d",
            "--no-deps",
            "--scale",
            f"worker={count}",
            "worker",
            timeout=120,
        )
        return self.wait_service_healthy("worker", count=count, timeout=120)

    def host_environment(self) -> dict[str, Any]:
        memory_bytes: int | None = None
        with contextlib.suppress(OSError, TypeError, ValueError):
            memory_bytes = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))

        docker_raw = self.run_command(
            ["docker", "info", "--format", "{{json .}}"],
            timeout=30,
            record=False,
        ).stdout
        try:
            docker_info = json.loads(docker_raw)
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("docker info did not return valid JSON") from exc
        return {
            "host": {
                "operating_system": platform.system(),
                "os_release": platform.release(),
                "architecture": platform.machine(),
                "cpu_model": platform.processor() or "unknown",
                "logical_cpu_count": os.cpu_count(),
                "memory_bytes": memory_bytes,
                "python_version": platform.python_version(),
            },
            "docker": {
                "server_version": docker_info.get("ServerVersion"),
                "operating_system": docker_info.get("OperatingSystem"),
                "architecture": docker_info.get("Architecture"),
                "logical_cpu_count": docker_info.get("NCPU"),
                "memory_bytes": docker_info.get("MemTotal"),
                "rootless": (docker_info.get("SecurityOptions") or []).__contains__(
                    "name=rootless"
                ),
            },
        }

    def container_resources(self, service: str) -> list[dict[str, Any]]:
        resources = []
        image_content_hashes: dict[str, str] = {}
        for container_id in self.service_container_ids(service):
            completed = self.run_command(
                ["docker", "inspect", container_id], timeout=20, record=False
            )
            try:
                inspected = json.loads(completed.stdout)[0]
            except (IndexError, KeyError, json.JSONDecodeError) as exc:
                raise AcceptanceFailure("unexpected docker inspect output") from exc
            labels = inspected.get("Config", {}).get("Labels") or {}
            self.require(
                labels.get("com.docker.compose.project") == self.project,
                "container resource snapshot escaped Compose project isolation",
            )
            limits = inspected.get("HostConfig") or {}
            image_id = str(inspected.get("Image") or "")
            self.require(_is_sha256_identity(image_id), "container image identity was missing")
            if image_id not in image_content_hashes:
                image_completed = self.run_command(
                    ["docker", "image", "inspect", image_id],
                    timeout=20,
                    record=False,
                )
                try:
                    inspected_image = json.loads(image_completed.stdout)[0]
                except (IndexError, KeyError, json.JSONDecodeError) as exc:
                    raise AcceptanceFailure("unexpected docker image inspect output") from exc
                image_content_hashes[image_id] = image_content_sha256(inspected_image)
            resources.append(
                {
                    "container_id": container_id,
                    "service": service,
                    "image_id": image_id,
                    "image_content_sha256": image_content_hashes[image_id],
                    "memory_limit_bytes": int(limits.get("Memory") or 0),
                    "memory_swap_limit_bytes": int(limits.get("MemorySwap") or 0),
                    "nano_cpus": int(limits.get("NanoCpus") or 0),
                    "cpu_quota": int(limits.get("CpuQuota") or 0),
                    "cpu_period": int(limits.get("CpuPeriod") or 0),
                    "pids_limit": limits.get("PidsLimit"),
                }
            )
        return resources

    def runtime_settings(self) -> dict[str, int | float]:
        """Read back the qualification-sensitive Settings inside the API container."""

        fields = (
            "database_pool_size",
            "database_max_overflow",
            "database_pool_timeout_seconds",
            "readiness_database_timeout_seconds",
            "worker_lease_seconds",
            "worker_heartbeat_seconds",
            "worker_poll_seconds",
            "worker_max_attempts",
            "worker_retry_backoff_base_seconds",
            "worker_retry_backoff_cap_seconds",
            "worker_shutdown_grace_seconds",
            "redis_block_milliseconds",
            "redis_operation_timeout_seconds",
        )
        source = (
            "import json; from app.core.config import get_settings; "
            "s=get_settings(); print(json.dumps({k:getattr(s,k) for k in "
            + repr(fields)
            + "}, sort_keys=True))"
        )
        completed = self.compose(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            source,
            timeout=30,
            record=False,
        )
        try:
            observed = json.loads(completed.stdout.strip())
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("runtime Settings read-back was not valid JSON") from exc
        expected: dict[str, int | float] = {
            "database_pool_size": DATABASE_POOL_SIZE,
            "database_max_overflow": DATABASE_MAX_OVERFLOW,
            "database_pool_timeout_seconds": DATABASE_POOL_TIMEOUT_SECONDS,
            "readiness_database_timeout_seconds": READINESS_DATABASE_TIMEOUT_SECONDS,
            "worker_lease_seconds": self.lease_seconds,
            "worker_heartbeat_seconds": self.heartbeat_seconds,
            "worker_poll_seconds": self.worker_poll_seconds,
            "worker_max_attempts": self.worker_max_attempts,
            "worker_retry_backoff_base_seconds": self.retry_backoff_base_seconds,
            "worker_retry_backoff_cap_seconds": self.retry_backoff_cap_seconds,
            "worker_shutdown_grace_seconds": self.worker_shutdown_grace_seconds,
            "redis_block_milliseconds": self.redis_block_milliseconds,
            "redis_operation_timeout_seconds": self.redis_operation_timeout_seconds,
        }
        self.require(set(observed) == set(expected), "runtime Settings field set drift", observed)
        for field, expected_value in expected.items():
            actual = observed[field]
            matches = (
                isinstance(expected_value, int)
                and not isinstance(expected_value, bool)
                and actual == expected_value
                and not isinstance(actual, bool)
            ) or (
                isinstance(expected_value, float)
                and not isinstance(actual, bool)
                and isinstance(actual, (int, float))
                and math.isclose(float(actual), expected_value, rel_tol=0, abs_tol=1e-12)
            )
            self.require(
                matches,
                "runtime Settings did not match the qualification profile",
                {"field": field, "expected": expected_value, "actual": actual},
            )
        return observed

    def apply_capacity_policy(self) -> dict[str, Any]:
        requested = finite_capacity_policy(
            backlog_limit=self.backlog_limit,
            question_quantum=self.question_quantum,
        )
        applied = self.http_json(
            "PUT",
            "/governance/policy",
            body=requested,
            accepted={200},
            timeout=15,
        )["payload"]
        read_back = self.http_json(
            "GET",
            "/governance/policy",
            accepted={200},
            timeout=10,
        )["payload"]
        self.require(
            applied["id"] == read_back["id"] and applied["policy_hash"] == read_back["policy_hash"],
            "capacity policy read-back did not match its API application",
            {"applied": applied, "read_back": read_back},
        )
        for field, expected in requested.items():
            actual = read_back.get(field)
            if actual is None:
                matches = False
            elif field.endswith("_usd"):
                matches = float(actual) == float(expected)
            else:
                matches = actual == expected
            self.require(
                matches,
                "capacity policy was not fully finite after API application",
                {"field": field, "expected": expected, "actual": actual},
            )
        self.policy_document = dict(read_back)
        return {
            "id": read_back["id"],
            "version": read_back["version"],
            "policy_hash": read_back["policy_hash"],
            "is_active": read_back["is_active"],
            "activated_at": read_back["activated_at"],
            "limits": requested,
        }

    def create_mock_capacity_model(self, role: str) -> dict[str, Any]:
        result = self.http_json(
            "POST",
            "/models",
            body={
                "name": f"Phase 2 Capacity {role} Mock {self.project[-12:]}",
                "provider_type": "mock",
                "enabled": True,
                "input_price_per_million": 0,
                "output_price_per_million": 0,
                "default_parameters": {
                    "temperature": 0,
                    "top_p": 1,
                    "max_tokens": RUN_MAX_TOKENS,
                    "seed": 42,
                },
            },
            accepted={201},
            timeout=15,
        )["payload"]
        self.require(
            result["provider_type"] == "mock"
            and result.get("api_key_env") is None
            and result.get("base_url") is None,
            "capacity fairness model escaped the Mock-only boundary",
            result,
        )
        return result

    def topology_and_data(self) -> None:
        postgres = self.wait_service_healthy("postgres")
        redis = self.wait_service_healthy("redis")
        api = self.wait_service_healthy("api")
        workers = self.wait_service_healthy("worker", count=self.worker_count)
        ready = self.wait_api_ready(200)
        demo = self.initialize_demo()
        policy = self.apply_capacity_policy()
        low_volume_model = self.create_mock_capacity_model("Low Volume")
        self.low_volume_model_id = low_volume_model["id"]
        postgres_version = self.psql("SHOW server_version;").stdout.strip()
        postgres_max_connections = int(self.psql("SHOW max_connections;").stdout.strip())
        redis_info = self.redis_cli("INFO", "server").stdout.splitlines()
        redis_version = next(
            (
                line.split(":", 1)[1].strip()
                for line in redis_info
                if line.startswith("redis_version:")
            ),
            "unknown",
        )
        runtime_settings = self.runtime_settings()
        self.evidence["environment"] = {
            **self.host_environment(),
            "postgres_version": postgres_version,
            "postgres_max_connections": postgres_max_connections,
            "redis_version": redis_version,
            "runtime_settings": runtime_settings,
            "container_limits": {
                "postgres": self.container_resources("postgres"),
                "redis": self.container_resources("redis"),
                "api": self.container_resources("api"),
                "workers": self.container_resources("worker"),
            },
        }
        self.evidence["data"] = {
            "benchmark": demo["benchmark"],
            "model": demo["model"],
            "fairness_low_volume_model": {
                "id": low_volume_model["id"],
                "provider_type": low_volume_model["provider_type"],
                "enabled": low_volume_model["enabled"],
                "api_key_env": low_volume_model.get("api_key_env"),
            },
            "governance_policy": policy,
            "protocol_version": "llmbenchlab-protocol-v1",
            "demo_only": True,
        }
        self.evidence["topology"] = {
            "postgres": postgres,
            "redis": redis,
            "api": api,
            "workers": workers,
            "ready": ready["payload"],
        }
        self.write_evidence()

    def create_capacity_run(self, *, model_id: str | None = None) -> dict[str, Any]:
        selected_model_id = model_id or self.model_id
        self.require(selected_model_id is not None, "Mock model was not initialized")
        self.require(self.benchmark_id is not None, "Demo benchmark was not initialized")
        result = self.http_json(
            "POST",
            "/runs",
            body={
                "model_id": selected_model_id,
                "benchmark_id": self.benchmark_id,
                "temperature": 0,
                "top_p": 1,
                "max_tokens": RUN_MAX_TOKENS,
                "seed": 42,
                "concurrency": self.run_concurrency,
                "input_token_reservation": RUN_INPUT_TOKEN_RESERVATION,
                "lifetime_request_budget": RUN_LIFETIME_REQUEST_BUDGET,
                "lifetime_token_budget": RUN_LIFETIME_TOKEN_BUDGET,
                "lifetime_cost_budget_usd": RUN_LIFETIME_COST_BUDGET_USD,
            },
            accepted={202, 429},
            timeout=10,
        )
        return {
            "status_code": result["status_code"],
            "elapsed_seconds": result["elapsed_seconds"],
            "payload": result["payload"],
        }

    def submit_runs(self, count: int, *, model_id: str | None = None) -> dict[str, Any]:
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.submit_concurrency, count)
        ) as executor:
            futures = [
                executor.submit(self.create_capacity_run, model_id=model_id) for _ in range(count)
            ]
            submissions = [future.result() for future in futures]
        duration = time.monotonic() - started
        return summarize_submissions(submissions, duration_seconds=duration)

    def task_metrics(self) -> dict[str, Any]:
        return self.http_json("GET", "/tasks/metrics", accepted={200})["payload"]

    def run_audit_events(self, run_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.http_json(
                "GET",
                f"/runs/{run_id}/audit?offset={offset}&limit=100",
                accepted={200},
                timeout=10,
            )["payload"]
            events.extend(page["items"])
            offset += len(page["items"])
            if offset >= int(page["total"]):
                return events
            self.require(bool(page["items"]), "Run audit pagination made no progress", page)

    @staticmethod
    def update_metric_peaks(peaks: dict[str, int], sample: dict[str, Any]) -> None:
        for key, value in sample.items():
            if key == "timestamp" or isinstance(value, bool) or not isinstance(value, int):
                continue
            peaks[key] = max(peaks.get(key, value), value)

    def wait_runs_terminal(
        self,
        run_ids: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        deadline = time.monotonic() + (timeout or self.timeout_seconds)
        remaining = set(run_ids)
        final: dict[str, dict[str, Any]] = {}
        peaks: dict[str, int] = {}
        samples = 0
        while remaining and time.monotonic() < deadline:
            for run_id in tuple(sorted(remaining)):
                run = self.get_run(run_id)
                if run["status"] in TERMINAL_STATUSES:
                    final[run_id] = run
                    remaining.remove(run_id)
            metrics = self.task_metrics()
            self.update_metric_peaks(peaks, metrics)
            samples += 1
            if remaining:
                time.sleep(0.1)
        self.require(
            not remaining,
            "capacity Runs did not reach terminal state",
            {"remaining_run_ids": sorted(remaining), "timeout_seconds": timeout},
        )
        return [final[run_id] for run_id in run_ids], {
            "sample_count": samples,
            "peak_database_gauges": peaks,
            "final_database_gauges": self.task_metrics(),
        }

    def database_pressure(self) -> dict[str, Any]:
        raw = self.psql(
            """
SELECT json_build_object(
  'captured_at', CURRENT_TIMESTAMP,
  'database_size_bytes', pg_database_size(current_database()),
  'max_connections', current_setting('max_connections')::integer,
  'connections', (
    SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()
  ),
  'provider_reservations', (SELECT count(*) FROM provider_call_reservations),
  'settled_actual_reservations', (
    SELECT count(*) FROM provider_call_reservations WHERE state = 'settled_actual'
  ),
  'settled_conservative_reservations', (
    SELECT count(*) FROM provider_call_reservations WHERE state = 'settled_conservative'
  ),
  'xact_commit', s.xact_commit,
  'xact_rollback', s.xact_rollback,
  'blks_read', s.blks_read,
  'blks_hit', s.blks_hit,
  'tup_returned', s.tup_returned,
  'tup_fetched', s.tup_fetched,
  'tup_inserted', s.tup_inserted,
  'tup_updated', s.tup_updated,
  'tup_deleted', s.tup_deleted,
  'conflicts', s.conflicts,
  'deadlocks', s.deadlocks,
  'temp_files', s.temp_files,
  'temp_bytes', s.temp_bytes
)::text
FROM pg_stat_database s
WHERE s.datname = current_database();
"""
        ).stdout.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("PostgreSQL pressure snapshot was not valid JSON") from exc

    @staticmethod
    def pressure_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
        counters = (
            "xact_commit",
            "xact_rollback",
            "blks_read",
            "blks_hit",
            "tup_returned",
            "tup_fetched",
            "tup_inserted",
            "tup_updated",
            "tup_deleted",
            "conflicts",
            "deadlocks",
            "temp_files",
            "temp_bytes",
        )
        return {key: int(after.get(key) or 0) - int(before.get(key) or 0) for key in counters}

    def queue_pressure(self) -> dict[str, Any]:
        stream = self.redis_mapping(self.redis_json("XINFO", "STREAM", TASK_STREAM))
        return {
            "stream_length": int(stream.get("length") or 0),
            "entries_added": int(stream.get("entries-added") or 0),
            "last_generated_id": str(stream.get("last-generated-id") or "0-0"),
            "group": self.group_info(),
        }

    def phase_result(
        self,
        *,
        name: str,
        workers: int,
        submissions: dict[str, Any],
        final_runs: Sequence[dict[str, Any]],
        elapsed_seconds: float,
        metrics: dict[str, Any],
        database_before: dict[str, Any],
        database_after: dict[str, Any],
        queue_before: dict[str, Any],
        queue_after: dict[str, Any],
        audit_events: dict[str, Sequence[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        completed = [run for run in final_runs if run["status"] == "completed"]
        question_latencies: list[float] = []
        question_errors = 0
        response_count = 0
        for run in final_runs:
            response_page = self.responses(run["id"])
            response_count += int(response_page["total"])
            for response in response_page["items"]:
                if response.get("latency_ms") is not None:
                    question_latencies.append(float(response["latency_ms"]))
                if response.get("error_type") is not None:
                    question_errors += 1

        queue_latencies = []
        execution_latencies = []
        end_to_end_latencies = []
        for run in final_runs:
            created = parse_datetime(run["created_at"])
            started = parse_datetime(run["started_at"]) if run.get("started_at") else None
            finished = parse_datetime(run["finished_at"]) if run.get("finished_at") else None
            if started is not None:
                queue_latencies.append((started - created).total_seconds())
            if started is not None and finished is not None:
                execution_latencies.append((finished - started).total_seconds())
            if finished is not None:
                end_to_end_latencies.append((finished - created).total_seconds())

        terminal_statuses: dict[str, int] = {}
        for run in final_runs:
            status = str(run["status"])
            terminal_statuses[status] = terminal_statuses.get(status, 0) + 1
        total_questions = sum(int(run["completed_questions"]) for run in final_runs)
        failure_retries = sum(int(run.get("failed_attempt_count") or 0) for run in final_runs)
        audit_events = audit_events or {
            run["id"]: self.run_audit_events(run["id"]) for run in final_runs
        }
        scheduling = cooperative_scheduling_summary(final_runs, audit_events)
        self.require(
            scheduling["all_runs_dispatched_more_than_once"] and scheduling["all_runs_yielded"],
            "finite question quantum did not produce cooperative scheduling evidence",
            scheduling,
        )
        queue_distribution: dict[str, Any] = {
            **distribution(queue_latencies),
            "samples": [round(value, 6) for value in queue_latencies],
        }
        execution_distribution: dict[str, Any] = {
            **distribution(execution_latencies),
            "samples": [round(value, 6) for value in execution_latencies],
        }
        end_to_end_distribution: dict[str, Any] = {
            **distribution(end_to_end_latencies),
            "samples": [round(value, 6) for value in end_to_end_latencies],
        }
        question_distribution: dict[str, Any] = {
            **distribution(question_latencies),
            "samples": [round(value, 6) for value in question_latencies],
        }
        attempt_delta = {
            field: int(database_after.get(field) or 0) - int(database_before.get(field) or 0)
            for field in (
                "provider_reservations",
                "settled_actual_reservations",
                "settled_conservative_reservations",
            )
        }
        self.require(
            attempt_delta
            == {
                "provider_reservations": total_questions,
                "settled_actual_reservations": total_questions,
                "settled_conservative_reservations": 0,
            },
            "measurement Provider-attempt ledger delta was not exactly one actual per question",
            attempt_delta,
        )
        serialized_wall_duration = round(elapsed_seconds, 6)
        self.require(
            serialized_wall_duration > 0,
            "measurement wall duration rounded to zero",
            elapsed_seconds,
        )
        return {
            "name": name,
            "workers": workers,
            "submission": {
                "requested": submissions["requested"],
                "accepted": len(submissions["accepted"]),
                "rejected": len(submissions["rejected"]),
                "status_counts": submissions["status_counts"],
                "duration_seconds": submissions["duration_seconds"],
                "request_latency_seconds": submissions["request_latency_seconds"],
            },
            "wall_duration_seconds": serialized_wall_duration,
            "throughput": {
                "runs_per_second": round(len(completed) / serialized_wall_duration, 6),
                "questions_per_second": round(total_questions / serialized_wall_duration, 6),
                "completed_runs": len(completed),
                "completed_questions": total_questions,
            },
            "latency_seconds": {
                "queue": queue_distribution,
                "execution": execution_distribution,
                "end_to_end": end_to_end_distribution,
            },
            "question_latency_ms": question_distribution,
            "errors_and_retries": {
                "terminal_statuses": terminal_statuses,
                "question_errors": question_errors,
                "failed_attempt_count": failure_retries,
                "lease_acquisitions": sum(int(run["attempt_count"]) for run in final_runs),
                "dispatches": sum(int(run.get("dispatch_count") or 0) for run in final_runs),
            },
            "provider_attempts": {
                **attempt_delta,
                "attempts_per_completed_question": (
                    attempt_delta["provider_reservations"] / total_questions
                ),
            },
            "cooperative_scheduling": scheduling,
            "response_count": response_count,
            "database": {
                "before": database_before,
                "after": database_after,
                "counter_delta": self.pressure_delta(database_before, database_after),
                "task_metrics": metrics,
            },
            "queue": {"before": queue_before, "after": queue_after},
            "run_ids": [run["id"] for run in final_runs],
        }

    def run_measurement_phase(self, name: str, workers: int) -> dict[str, Any]:
        worker_state = self.scale_workers(workers)
        database_before = self.database_pressure()
        queue_before = self.queue_pressure()
        started = time.monotonic()
        submissions = self.submit_runs(self.runs_per_phase)
        self.require(
            not submissions["rejected"],
            "capacity measurement unexpectedly hit admission rejection",
            submissions["rejected"],
        )
        run_ids = [run["id"] for run in submissions["accepted"]]
        final_runs, metrics = self.wait_runs_terminal(run_ids)
        self.require(
            len(final_runs) == len(run_ids)
            and all(run["status"] == "completed" for run in final_runs),
            "capacity measurement did not complete every accepted Run",
            final_runs,
        )
        elapsed = time.monotonic() - started
        self.wait_queue_drained(timeout=90)
        database_after = self.database_pressure()
        queue_after = self.queue_pressure()
        result = self.phase_result(
            name=name,
            workers=workers,
            submissions=submissions,
            final_runs=final_runs,
            elapsed_seconds=elapsed,
            metrics=metrics,
            database_before=database_before,
            database_after=database_after,
            queue_before=queue_before,
            queue_after=queue_after,
        )
        result["worker_state"] = worker_state
        self.evidence["measurements"].append(result)
        self.write_evidence()
        return result

    def _validated_running_workers(self) -> list[dict[str, Any]]:
        workers = self.service_metas("worker")
        self.require(
            len(workers) == self.worker_count
            and all(
                worker.get("project") == self.project
                and worker.get("service") == "worker"
                and worker.get("status") == "running"
                and worker.get("health") == "healthy"
                for worker in workers
            ),
            "backlog admission scenario requires the configured healthy Workers",
            workers,
        )
        return workers

    def _assert_worker_pause_state(
        self,
        workers: Sequence[dict[str, Any]],
        *,
        expected: bool,
    ) -> None:
        completed = self.run_command(
            ["docker", "inspect", *[str(worker["id"]) for worker in workers]],
            timeout=20,
            record=False,
        )
        try:
            inspected = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("Worker pause-state inspect was not valid JSON") from exc
        by_id = {item.get("Id"): item for item in inspected if isinstance(item, dict)}
        expected_status = "paused" if expected else "running"
        for worker in workers:
            item = by_id.get(worker["id"])
            labels = ((item or {}).get("Config") or {}).get("Labels") or {}
            state = (item or {}).get("State") or {}
            self.require(
                labels.get("com.docker.compose.project") == self.project
                and labels.get("com.docker.compose.service") == "worker"
                and state.get("Status") == expected_status
                and state.get("Paused") is expected,
                "Worker pause-state verification failed",
            )

    def _pause_workers(self, workers: Sequence[dict[str, Any]]) -> None:
        for worker in workers:
            self.run_command(["docker", "pause", str(worker["id"])], timeout=30)
        self._assert_worker_pause_state(workers, expected=True)

    def _unpause_workers(self, workers: Sequence[dict[str, Any]]) -> None:
        for worker in workers:
            self.run_command(["docker", "unpause", str(worker["id"])], timeout=30)
        self._assert_worker_pause_state(workers, expected=False)

    def _recover_unpaused_workers(self, workers: Sequence[dict[str, Any]]) -> None:
        for worker in workers:
            # Continue across the full validated set; the exact state read-back below
            # is authoritative even if one idempotent recovery command timed out.
            with contextlib.suppress(AcceptanceFailure):
                self.run_command(
                    ["docker", "unpause", str(worker["id"])],
                    timeout=30,
                    check=False,
                )
        self._assert_worker_pause_state(workers, expected=False)

    def _validate_burst_submissions(self, submissions: dict[str, Any]) -> tuple[list[str], int]:
        expected_rejections = self.burst_runs - self.backlog_limit
        self.require(
            submissions["status_counts"] == {"202": self.backlog_limit, "429": expected_rejections}
            and len(submissions["accepted"]) == self.backlog_limit
            and len(submissions["rejected"]) == expected_rejections,
            "concurrent backlog admission did not return the exact 202/429 split",
            submissions,
        )
        for rejected in submissions["rejected"]:
            detail = rejected["payload"].get("detail", {})
            self.require(
                rejected["status_code"] == 429
                and detail.get("code") == "run_backlog_full"
                and int(detail.get("limit", -1)) == self.backlog_limit,
                "backlog rejection did not preserve the typed local admission reason",
                rejected,
            )
        run_ids = [str(run["id"]) for run in submissions["accepted"]]
        self.require(
            len(run_ids) == len(set(run_ids)) == self.backlog_limit,
            "accepted backlog submissions did not produce unique durable Run IDs",
            run_ids,
        )
        return run_ids, expected_rejections

    def _backlog_burst(
        self,
        *,
        name: str,
        barrier: str,
        require_worker_participation: bool,
    ) -> dict[str, Any]:
        running_workers = self._validated_running_workers()
        self.wait_queue_drained(timeout=90)
        initial_metrics = self.task_metrics()
        self.require(
            int(initial_metrics["managed_backlog"]) == 0,
            "backlog admission scenario did not start from an empty managed backlog",
            initial_metrics,
        )

        pause_may_be_active = False
        try:
            suspend_started = time.monotonic()
            if barrier == "warmed_pause":
                pause_may_be_active = True
                self._pause_workers(running_workers)
                suspended_workers = list(running_workers)
            elif barrier == "cold_start":
                self.compose("stop", "worker", timeout=120)
                suspended_workers = self.service_metas("worker", include_stopped=True)
                self.require(
                    len(suspended_workers) == self.worker_count
                    and all(
                        worker.get("project") == self.project
                        and worker.get("service") == "worker"
                        and worker.get("status") == "exited"
                        for worker in suspended_workers
                    ),
                    "cold burst did not stop every validated Worker",
                    suspended_workers,
                )
            else:  # pragma: no cover - internal caller contract
                raise AcceptanceFailure("unsupported backlog barrier")
            suspend_seconds = time.monotonic() - suspend_started
            suspend_metrics = self.task_metrics()
            database_before = self.database_pressure()
            queue_before = self.queue_pressure()

            wall_started = time.monotonic()
            submissions = self.submit_runs(self.burst_runs)
            run_ids, expected_rejections = self._validate_burst_submissions(submissions)
            pending = [self.get_run(run_id) for run_id in run_ids]
            self.require(
                all(
                    run["status"] == "pending" and run["completed_questions"] == 0
                    for run in pending
                ),
                "suspended-Worker burst did not remain durably pending",
                pending,
            )
            backlog_metrics = self.task_metrics()
            self.require(
                int(backlog_metrics["managed_backlog"]) == self.backlog_limit
                and int(backlog_metrics["pending"]) == self.backlog_limit
                and int(backlog_metrics["running"]) == 0,
                "database backlog gauge did not reach the exact configured limit",
                backlog_metrics,
            )
            backlog_build_seconds = time.monotonic() - wall_started

            drain_started = time.monotonic()
            restore_started = drain_started
            if barrier == "warmed_pause":
                self._unpause_workers(suspended_workers)
                pause_may_be_active = False
            else:
                self.start_validated_containers(suspended_workers, expected_service="worker")
            restore_command_seconds = time.monotonic() - restore_started
            restore_metrics = self.task_metrics()
            worker_state = self.wait_service_healthy("worker", count=self.worker_count, timeout=120)
            final_runs, metrics = self.wait_runs_terminal(run_ids)
            drain_seconds = time.monotonic() - drain_started
            self.require(
                {str(run["id"]) for run in final_runs} == set(run_ids)
                and all(
                    run["status"] == "completed"
                    and int(run["completed_questions"]) == DEMO_QUESTIONS_PER_RUN
                    for run in final_runs
                ),
                "an accepted Run was lost or incomplete while draining the full backlog",
                final_runs,
            )
            elapsed = time.monotonic() - wall_started
            self.wait_queue_drained(timeout=90)
            drained_metrics = self.task_metrics()
            self.require(
                int(drained_metrics["managed_backlog"]) == 0
                and int(drained_metrics["pending"]) == 0
                and int(drained_metrics["running"]) == 0,
                "accepted backlog did not drain to zero",
                drained_metrics,
            )
            database_after = self.database_pressure()
            queue_after = self.queue_pressure()
            audit_events = {run_id: self.run_audit_events(run_id) for run_id in run_ids}
            result = self.phase_result(
                name=name,
                workers=self.worker_count,
                submissions=submissions,
                final_runs=final_runs,
                elapsed_seconds=elapsed,
                metrics=metrics,
                database_before=database_before,
                database_after=database_after,
                queue_before=queue_before,
                queue_after=queue_after,
                audit_events=audit_events,
            )
            result.update(
                {
                    "mode": "exact_backlog_limit_concurrent_202_429_then_drain",
                    "local_admission_boundary": (
                        "Database-serialized local Run admission only; this is not Provider "
                        "admission, Provider billing truth, or a Provider SLA."
                    ),
                    "configured_backlog_limit": self.backlog_limit,
                    "expected_status_counts": {
                        "202": self.backlog_limit,
                        "429": expected_rejections,
                    },
                    "observed_status_counts": submissions["status_counts"],
                    "typed_rejections": submissions["rejected"],
                    "accepted_run_ids": run_ids,
                    "accepted_runs_preserved": {str(run["id"]) for run in final_runs}
                    == set(run_ids),
                    "backlog_drain_seconds": round(drain_seconds, 6),
                    "backlog_at_configured_limit": backlog_metrics,
                    "backlog_after_drain": drained_metrics,
                    "worker_state_after_restart": worker_state,
                }
            )
            if require_worker_participation:
                result["barrier"] = barrier
                participation = burst_worker_participation(
                    accepted_run_ids=run_ids,
                    audit_events=audit_events,
                    worker_state=worker_state,
                    project=self.project,
                    backlog_ready_at=str(backlog_metrics["timestamp"]),
                )
                result["worker_state_after_restart"] = formal_worker_state_witness(worker_state)
                result["worker_participation"] = participation
                result["timing"] = burst_segmented_timing(
                    final_runs=final_runs,
                    audit_events=audit_events,
                    participation=participation,
                    suspend_completed_at=str(suspend_metrics["timestamp"]),
                    backlog_ready_at=str(backlog_metrics["timestamp"]),
                    restore_completed_at=str(restore_metrics["timestamp"]),
                    suspend_seconds=suspend_seconds,
                    backlog_build_seconds=backlog_build_seconds,
                    restore_command_seconds=restore_command_seconds,
                    drain_seconds=drain_seconds,
                )
            self.evidence["measurements"].append(result)
            self.write_evidence()
            return result
        finally:
            if pause_may_be_active:
                self._recover_unpaused_workers(running_workers)

    def bounded_queue_burst(self) -> dict[str, Any]:
        """Preserve the default capacity profile's single cold burst."""

        return self._backlog_burst(
            name="bounded_queue_burst_and_drain",
            barrier="cold_start",
            require_worker_participation=False,
        )

    def warmed_pause_burst(self) -> dict[str, Any]:
        return self._backlog_burst(
            name="warmed_pause_burst_and_drain",
            barrier="warmed_pause",
            require_worker_participation=True,
        )

    def cold_start_burst(self) -> dict[str, Any]:
        return self._backlog_burst(
            name="cold_start_burst_and_drain",
            barrier="cold_start",
            require_worker_participation=True,
        )

    def model_fairness_scenario(self) -> dict[str, Any]:
        self.require(self.model_id is not None, "high-volume Mock model was not initialized")
        self.require(
            self.low_volume_model_id is not None,
            "low-volume Mock model was not initialized",
        )
        self.wait_queue_drained(timeout=90)
        metrics_before = self.task_metrics()
        self.require(
            int(metrics_before["managed_backlog"]) == 0,
            "fairness scenario did not start from an empty managed backlog",
            metrics_before,
        )
        self.compose("stop", "worker", timeout=120)
        stopped_workers = self.service_metas("worker", include_stopped=True)
        self.require(
            bool(stopped_workers)
            and all(worker["status"] == "exited" for worker in stopped_workers),
            "fairness scenario did not stop every Worker",
            stopped_workers,
        )

        high_run_count = self.backlog_limit - 1
        high_submissions = self.submit_runs(high_run_count, model_id=self.model_id)
        self.require(
            high_submissions["status_counts"] == {"202": high_run_count}
            and not high_submissions["rejected"],
            "high-volume Model backlog was not admitted",
            high_submissions,
        )
        low_submission = self.create_capacity_run(model_id=self.low_volume_model_id)
        self.require(
            low_submission["status_code"] == 202,
            "low-volume Model Run was not admitted at the final backlog slot",
            low_submission,
        )
        high_run_ids = [str(run["id"]) for run in high_submissions["accepted"]]
        low_run_id = str(low_submission["payload"]["id"])
        backlog_metrics = self.task_metrics()
        self.require(
            int(backlog_metrics["managed_backlog"]) == self.backlog_limit
            and int(backlog_metrics["pending"]) == self.backlog_limit
            and int(backlog_metrics["running"]) == 0,
            "fairness scenario did not fill the configured local backlog",
            backlog_metrics,
        )

        one_worker = self.scale_workers(1)

        def fairness_observation() -> dict[str, Any]:
            low = self.get_run(low_run_id)
            high = [self.get_run(run_id) for run_id in high_run_ids]
            selected_fields = (
                "id",
                "model_id",
                "status",
                "completed_questions",
                "dispatch_count",
                "last_scheduled_at",
            )
            return {
                "low_run": {field: low.get(field) for field in selected_fields},
                "high_runs": [{field: run.get(field) for field in selected_fields} for run in high],
            }

        observation = self.wait_for(
            "low-volume Model to receive a claimed execution slice",
            fairness_observation,
            lambda snapshot: (
                int(snapshot["low_run"].get("dispatch_count") or 0) >= 1
                and int(snapshot["low_run"].get("completed_questions") or 0) >= 1
            ),
            timeout=self.timeout_seconds,
            interval=0.05,
        )
        self.require(
            all(run["status"] not in TERMINAL_STATUSES for run in observation["high_runs"]),
            "low-volume slice was not observed before the high-volume backlog completed",
            observation,
        )

        all_run_ids = [*high_run_ids, low_run_id]
        final_runs, final_metrics = self.wait_runs_terminal(all_run_ids)
        self.require(
            {run["id"] for run in final_runs} == set(all_run_ids)
            and all(run["status"] == "completed" for run in final_runs),
            "fairness scenario did not drain every accepted Run",
            final_runs,
        )
        self.wait_queue_drained(timeout=90)
        audit_events = {run_id: self.run_audit_events(run_id) for run_id in all_run_ids}
        ordering = fairness_ordering_summary(
            high_run_ids=high_run_ids,
            low_run_id=low_run_id,
            audit_events=audit_events,
            observation=observation,
        )
        self.require(
            ordering["low_volume_claim_observed"]
            and ordering["low_volume_slice_observed"]
            and ordering["high_volume_incomplete_at_low_slice"] == high_run_count
            and ordering["low_claim_before_high_backlog_drained"],
            "durable audit ordering did not prove cross-Model fair scheduling",
            ordering,
        )
        restored_workers = self.scale_workers(self.worker_count)
        drained_metrics = self.task_metrics()
        self.require(
            int(drained_metrics["managed_backlog"]) == 0,
            "fairness backlog did not drain to zero",
            drained_metrics,
        )
        result = {
            "name": "cross_model_fair_quantum_ordering",
            "boundary": (
                "Observed local database scheduling for two Mock Models only; this is not "
                "Provider-side fairness, capacity, or an SLA."
            ),
            "question_quantum": self.question_quantum,
            "configured_backlog_limit": self.backlog_limit,
            "high_volume_model_id": self.model_id,
            "low_volume_model_id": self.low_volume_model_id,
            "high_volume_run_ids": high_run_ids,
            "low_volume_run_id": low_run_id,
            "backlog_at_start": backlog_metrics,
            "ordering_evidence": ordering,
            "terminal_runs": [
                {
                    "id": run["id"],
                    "model_id": run["model_id"],
                    "status": run["status"],
                    "completed_questions": run["completed_questions"],
                    "dispatch_count": run["dispatch_count"],
                }
                for run in final_runs
            ],
            "task_metrics": final_metrics,
            "backlog_after_drain": drained_metrics,
            "one_worker_state": one_worker,
            "restored_worker_state": restored_workers,
        }
        self.evidence["fairness"] = result
        self.write_evidence()
        return result

    def lease_expiry_fault(self) -> dict[str, Any]:
        workers_by_hostname = {
            str(worker["hostname"]): worker for worker in self.service_metas("worker")
        }
        self.require(
            len(workers_by_hostname) == self.worker_count,
            "lease fault could not pre-cache the Worker container map",
            list(workers_by_hostname),
        )
        created = self.create_capacity_run()
        self.require(created["status_code"] == 202, "lease fault Run was not admitted", created)
        run_id = created["payload"]["id"]
        deadline = time.monotonic() + self.timeout_seconds
        frozen: dict[str, Any] | None = None
        victim: dict[str, Any] | None = None
        paused_container_id: str | None = None
        try:
            while time.monotonic() < deadline:
                observed = self.wait_for(
                    "active Mock lease before capacity Worker pause",
                    lambda: self.db_run_snapshot(run_id),
                    lambda run: (
                        run["status"] == "running"
                        and run.get("lease_owner") is not None
                        and int(run.get("send_started_provider_attempts") or 0) == 1
                        and int(run.get("response_count") or 0) < DEMO_QUESTIONS_PER_RUN
                    ),
                    timeout=max(0.1, deadline - time.monotonic()),
                    interval=0.02,
                )
                owner = str(observed["lease_owner"])
                owner_parts = owner.split(":")
                self.require(len(owner_parts) >= 4, "unexpected Worker lease owner", owner)
                candidate = workers_by_hostname.get(owner_parts[1])
                self.require(candidate is not None, "could not map lease owner to Worker", owner)
                paused_container_id = str(candidate["id"])
                self.run_command(["docker", "pause", paused_container_id], timeout=20)
                fenced = self.db_run_snapshot(run_id)
                if (
                    fenced["status"] == "running"
                    and fenced.get("lease_owner") == owner
                    and fenced.get("lease_token") == observed.get("lease_token")
                    and fenced.get("lease_expires_at") is not None
                    and int(fenced.get("send_started_provider_attempts") or 0) == 1
                ):
                    frozen = fenced
                    victim = candidate
                    break
                unpaused = self.run_command(
                    ["docker", "unpause", paused_container_id], timeout=20, check=False
                )
                self.require(
                    unpaused.returncode == 0,
                    "could not unpause an unstable Worker lease candidate",
                    {"container_id": paused_container_id, "returncode": unpaused.returncode},
                )
                paused_container_id = None
            self.require(
                frozen is not None and victim is not None,
                "could not freeze a stable Worker lease before SIGKILL",
            )
            owner = str(frozen["lease_owner"])
            before_ids = set(frozen["response_ids"])
            kill_fence_database_at = str(frozen["database_now"])
            old_lease_expires_at = str(frozen["lease_expires_at"])
            self.run_command(["docker", "kill", "--signal", "KILL", victim["id"]], timeout=30)
            paused_container_id = None
        finally:
            if paused_container_id is not None:
                with contextlib.suppress(Exception):
                    self.run_command(
                        ["docker", "unpause", paused_container_id],
                        timeout=20,
                        check=False,
                    )
        killed = self.container_meta(victim["id"])
        self.require(killed["status"] == "exited", "SIGKILL victim did not exit", killed)
        self.require(killed["exit_code"] != 0, "SIGKILL victim exited successfully", killed)
        post_kill_database = self.db_run_snapshot(run_id)
        self.require(
            post_kill_database["status"] == "running"
            and post_kill_database["lease_owner"] == owner
            and post_kill_database["lease_expires_at"] is not None,
            "SIGKILL did not preserve the durable lease until natural expiry",
            post_kill_database,
        )
        final_runs, metrics = self.wait_runs_terminal([run_id], timeout=self.timeout_seconds)
        final = final_runs[0]
        self.require(final["status"] == "completed", "lease expiry Run did not recover", final)
        self.require(
            int(final["attempt_count"]) >= 2 and int(final.get("failed_attempt_count") or 0) >= 1,
            "lease expiry did not consume one failure and reacquire",
            final,
        )
        responses = self.responses(run_id)["items"]
        final_ids = {response["id"] for response in responses}
        self.require(before_ids.issubset(final_ids), "lease recovery replaced durable Responses")
        self.require(
            len(responses) == len({response["question_id"] for response in responses}) == 15,
            "lease recovery produced duplicate or missing Response evidence",
        )
        audit_events = self.run_audit_events(run_id)
        try:
            reclaim = first_claim_at_or_after(audit_events, old_lease_expires_at)
            kill_fence_to_reclaim_seconds = nonnegative_utc_elapsed_seconds(
                kill_fence_database_at,
                str(reclaim["occurred_at"]),
            )
            lease_expiry_to_reclaim_seconds = nonnegative_utc_elapsed_seconds(
                old_lease_expires_at,
                str(reclaim["occurred_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AcceptanceFailure(
                "typed lease recovery timing was missing or not non-negative"
            ) from exc
        self.require(
            int(reclaim.get("lease_token") or 0) > int(frozen.get("lease_token") or 0),
            "first post-expiry claim did not advance the durable lease token",
            reclaim,
        )
        self.start_validated_containers([killed], expected_service="worker")
        worker_state = self.wait_service_healthy("worker", count=self.worker_count, timeout=120)
        fault = {
            "name": "lease_owner_sigkill_and_expiry_recovery",
            "run_id": run_id,
            "victim": victim,
            "victim_after_kill": killed,
            "partial_completed_questions": frozen["response_count"],
            "preserved_response_ids": sorted(before_ids),
            "post_kill_database": post_kill_database,
            "timing": {
                "kill_fence_database_at": kill_fence_database_at,
                "old_lease_expires_at": old_lease_expires_at,
                "reclaim_occurred_at": reclaim["occurred_at"],
                "kill_fence_to_reclaim_seconds": kill_fence_to_reclaim_seconds,
                "lease_expiry_to_reclaim_seconds": lease_expiry_to_reclaim_seconds,
            },
            "terminal": {
                **{field: final.get(field) for field in RUN_SNAPSHOT_FIELDS},
                "failed_attempt_count": final.get("failed_attempt_count"),
            },
            "task_metrics": metrics,
            "worker_state_after_restart": worker_state,
        }
        self.evidence["faults"].append(fault)
        self.write_evidence()
        return fault

    def redis_outage_fault(self) -> dict[str, Any]:
        self.compose("stop", "worker", timeout=120)
        stopped_workers = self.service_metas("worker", include_stopped=True)
        self.require(
            len(stopped_workers) == self.worker_count,
            "Redis fault did not stop the configured Worker count",
            stopped_workers,
        )
        self.compose("stop", "redis", timeout=60)
        degraded = self.wait_api_ready(503, timeout=45)
        created = self.create_capacity_run()
        self.require(created["status_code"] == 202, "Redis outage Run was not admitted", created)
        run_id = created["payload"]["id"]
        pending = self.get_run(run_id)
        self.require(
            pending["status"] == "pending"
            and pending.get("last_error") == "queue_notification_unavailable",
            "Redis outage did not preserve the durable database-first queue error",
            pending,
        )
        self.start_validated_containers(stopped_workers, expected_service="worker")
        workers_degraded = self.wait_service_healthy("worker", count=self.worker_count, timeout=120)
        final_runs, metrics = self.wait_runs_terminal([run_id], timeout=self.timeout_seconds)
        final = final_runs[0]
        self.require(
            final["status"] == "completed",
            "database scan did not complete Run while Redis was unavailable",
            final,
        )
        try:
            run_created_at = str(pending["created_at"])
            first_claim = first_claim_at_or_after(
                self.run_audit_events(run_id),
                run_created_at,
            )
            run_created_to_claim_seconds = nonnegative_utc_elapsed_seconds(
                run_created_at,
                str(first_claim["occurred_at"]),
            )
            run_created_to_terminal_seconds = nonnegative_utc_elapsed_seconds(
                run_created_at,
                str(final["finished_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AcceptanceFailure(
                "typed Redis-outage recovery timing was missing or not non-negative"
            ) from exc
        redis_stopped = self.service_metas("redis", include_stopped=True)[0]
        self.require(redis_stopped["status"] == "exited", "Redis recovered before DB scan")
        self.compose("start", "redis", timeout=60)
        redis_healthy = self.wait_service_healthy("redis", timeout=120)
        ready = self.wait_api_ready(200, timeout=60)
        fault = {
            "name": "redis_stop_start_database_reconciliation",
            "run_id": run_id,
            "post_elapsed_seconds": created["elapsed_seconds"],
            "ready_while_stopped": degraded["payload"],
            "pending_last_error": pending.get("last_error"),
            "workers_while_redis_stopped": workers_degraded,
            "terminal_status": final["status"],
            "timing": {
                "run_created_at": run_created_at,
                "first_claim_occurred_at": first_claim["occurred_at"],
                "terminal_at": final["finished_at"],
                "run_created_to_claim_seconds": run_created_to_claim_seconds,
                "run_created_to_terminal_seconds": run_created_to_terminal_seconds,
            },
            "task_metrics": metrics,
            "redis_after_start": redis_healthy,
            "ready_after_start": ready["payload"],
        }
        self.evidence["faults"].append(fault)
        self.write_evidence()
        return fault

    def duplicate_delivery_fault(self, run_id: str) -> dict[str, Any]:
        before = self.canonical_snapshot(run_id)
        duplicate_id = self.redis_cli(
            "XADD",
            TASK_STREAM,
            "*",
            "version",
            TASK_MESSAGE_VERSION,
            "run_id",
            run_id,
            "correlation_id",
            run_id,
        ).stdout.strip()
        self.require(bool(duplicate_id), "Redis did not accept duplicate notification")
        queue = self.wait_message_delivered_and_acked(duplicate_id, timeout=90)
        after = self.canonical_snapshot(run_id)
        self.require(
            before["sha256"] == after["sha256"],
            "duplicate delivery changed terminal Run/Response evidence",
            {"before": before["sha256"], "after": after["sha256"]},
        )
        fault = {
            "name": "duplicate_terminal_delivery_noop",
            "run_id": run_id,
            "duplicate_message_id": duplicate_id,
            "snapshot_sha256": before["sha256"],
            "before_snapshot_sha256": before["sha256"],
            "after_snapshot_sha256": after["sha256"],
            "queue_after_ack": queue,
        }
        self.evidence["faults"].append(fault)
        self.write_evidence()
        return fault

    def _backend_image_ids_before_cleanup(self) -> set[str]:
        image_ids: set[str] = set()
        for service in ("api", "migrate", "worker"):
            for metadata in self.service_metas(service, include_stopped=True):
                image_id = metadata.get("image_id")
                self.require(
                    metadata.get("project") == self.project
                    and metadata.get("service") == service
                    and _is_sha256_identity(image_id),
                    "backend container image identity failed cleanup validation",
                )
                image_ids.add(str(image_id))
        return image_ids

    def _project_image_ids(self) -> list[str]:
        completed = self.run_command(
            [
                "docker",
                "image",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--quiet",
                "--no-trunc",
            ],
            timeout=20,
            check=False,
            record=False,
        )
        if completed.returncode != 0:
            raise AcceptanceFailure("project image enumeration failed")
        image_ids = sorted(set(completed.stdout.split()))
        if not all(_is_sha256_identity(image_id) for image_id in image_ids):
            raise AcceptanceFailure("project image enumeration returned an invalid identity")
        return image_ids

    def _inspect_cleanup_image(self, image_id: str) -> dict[str, Any]:
        completed = self.run_command(
            ["docker", "image", "inspect", image_id],
            timeout=20,
            check=False,
            record=False,
        )
        if completed.returncode != 0:
            raise AcceptanceFailure("project image inspection failed")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("project image inspection was not valid JSON") from exc
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], dict)
            or payload[0].get("Id") != image_id
        ):
            raise AcceptanceFailure("project image inspection identity did not match")
        return payload[0]

    def cleanup(self) -> str | None:
        """Run generic cleanup first, then remove only this capacity build's exact tag."""

        image_counts: dict[str, Any] = {
            "image_cleanup_status": "not_attempted",
            "project_image_candidates": 0,
            "removed_project_images": 0,
            "retained_shared_project_images": 0,
            "remaining_project_images": None,
        }
        if not self.stack_touched or PROJECT_PATTERN.fullmatch(self.project) is None:
            base_error = super().cleanup()
            self.evidence.setdefault("cleanup", {}).update(image_counts)
            return base_error

        pre_cleanup_images: set[str] | None = None
        pre_cleanup_error = False
        try:
            pre_cleanup_images = self._backend_image_ids_before_cleanup()
        except (AcceptanceFailure, KeyError, TypeError, ValueError):
            pre_cleanup_error = True

        base_error = super().cleanup()
        cleanup_evidence = self.evidence.setdefault("cleanup", {})
        cleanup_evidence.update(image_counts)
        if base_error is not None:
            return base_error
        if (
            cleanup_evidence.get("remaining_containers")
            or cleanup_evidence.get("remaining_project_volumes")
            or cleanup_evidence.get("remaining_project_networks")
        ):
            return "isolated Compose project cleanup was incomplete"
        if pre_cleanup_error or pre_cleanup_images is None:
            cleanup_evidence["image_cleanup_status"] = "failed"
            return "capacity image cleanup precondition failed"

        try:
            candidates = self._project_image_ids()
        except AcceptanceFailure:
            cleanup_evidence["image_cleanup_status"] = "failed"
            return "capacity image cleanup enumeration failed"
        cleanup_evidence["project_image_candidates"] = len(candidates)
        cleanup_evidence["remaining_project_images"] = len(candidates)

        expected_tag = f"llmbenchlab-backend:p2-{self.project.rsplit('-', 1)[-1]}"
        unsafe_candidates = len(candidates) > 1
        inspected_candidates: dict[str, dict[str, Any]] = {}
        for image_id in candidates:
            try:
                inspected = self._inspect_cleanup_image(image_id)
            except AcceptanceFailure:
                unsafe_candidates = True
                continue
            inspected_candidates[image_id] = inspected
            config = inspected.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            repo_tags = inspected.get("RepoTags")
            if (
                not isinstance(labels, dict)
                or labels.get("com.docker.compose.project") != self.project
                or labels.get("com.docker.compose.service") not in {"api", "migrate", "worker"}
                or repo_tags != [expected_tag]
            ):
                unsafe_candidates = True
                continue
            try:
                references = self.run_command(
                    [
                        "docker",
                        "container",
                        "ls",
                        "-a",
                        "--filter",
                        f"ancestor={image_id}",
                        "--quiet",
                        "--no-trunc",
                    ],
                    timeout=20,
                    check=False,
                    record=False,
                )
            except AcceptanceFailure:
                cleanup_evidence["image_cleanup_status"] = "failed"
                cleanup_evidence["retained_shared_project_images"] = len(candidates)
                with contextlib.suppress(AcceptanceFailure):
                    cleanup_evidence["remaining_project_images"] = len(self._project_image_ids())
                return "capacity image cleanup reference verification failed"
            if references.returncode != 0 or references.stdout.split():
                unsafe_candidates = True

        if set(candidates) != pre_cleanup_images:
            unsafe_candidates = True
        if unsafe_candidates or len(inspected_candidates) != len(candidates):
            cleanup_evidence["image_cleanup_status"] = "failed"
            cleanup_evidence["retained_shared_project_images"] = len(candidates)
            with contextlib.suppress(AcceptanceFailure):
                cleanup_evidence["remaining_project_images"] = len(self._project_image_ids())
            return "capacity image cleanup safety validation failed"

        if candidates:
            try:
                removal = self.run_command(
                    ["docker", "image", "rm", expected_tag],
                    timeout=60,
                    check=False,
                    record=False,
                )
            except AcceptanceFailure:
                cleanup_evidence["image_cleanup_status"] = "failed"
                with contextlib.suppress(AcceptanceFailure):
                    cleanup_evidence["remaining_project_images"] = len(self._project_image_ids())
                return "capacity image cleanup removal failed"
            if removal.returncode != 0:
                cleanup_evidence["image_cleanup_status"] = "failed"
                with contextlib.suppress(AcceptanceFailure):
                    cleanup_evidence["remaining_project_images"] = len(self._project_image_ids())
                return "capacity image cleanup removal failed"

        try:
            remaining = self._project_image_ids()
            for image_id in remaining:
                inspected = self._inspect_cleanup_image(image_id)
                labels = (inspected.get("Config") or {}).get("Labels") or {}
                if labels.get("com.docker.compose.project") != self.project:
                    raise AcceptanceFailure("remaining project image label did not match")
        except (AcceptanceFailure, AttributeError):
            cleanup_evidence["image_cleanup_status"] = "failed"
            return "capacity image cleanup verification failed"
        cleanup_evidence["remaining_project_images"] = len(remaining)
        if remaining:
            cleanup_evidence["image_cleanup_status"] = "failed"
            return "capacity image cleanup left a project image"

        cleanup_evidence["removed_project_images"] = len(candidates)
        cleanup_evidence["image_cleanup_status"] = "passed"
        if getattr(self, "formal_slo_v2", False) and (
            cleanup_evidence["project_image_candidates"],
            cleanup_evidence["removed_project_images"],
            cleanup_evidence["retained_shared_project_images"],
            cleanup_evidence["remaining_project_images"],
        ) != (1, 1, 0, 0):
            cleanup_evidence["image_cleanup_status"] = "failed"
            return "formal capacity image cleanup counts were not exact"
        return None

    def governance_reconciliation(self) -> dict[str, Any]:
        raw = self.psql(GOVERNANCE_RECONCILIATION_SQL).stdout.strip()
        try:
            snapshot = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("governance reconciliation was not valid JSON") from exc
        if not isinstance(snapshot, dict):
            raise AcceptanceFailure("governance reconciliation must be a JSON object")
        expected_counts, expected_states, expected_audit_counts = reconciliation_expectations(
            runs_per_phase=self.runs_per_phase,
            backlog_limit=self.backlog_limit,
            formal_slo_v2=getattr(self, "formal_slo_v2", False),
        )
        validate_governance_reconciliation_snapshot(
            snapshot,
            expected_counts=expected_counts,
            expected_reservation_states=expected_states,
            expected_provider_attempt_audit_counts=expected_audit_counts,
        )
        return snapshot

    def run_all(self) -> None:
        self.evidence["status"] = "running"
        self.write_evidence()
        self.setup_stack()
        self.topology_and_data()
        measurement_cells = {
            "single_worker_reference": 1,
            "configured_multi_worker_baseline": self.worker_count,
        }
        ordered_names = (
            ("single_worker_reference", "configured_multi_worker_baseline")
            if self.measurement_order == "single_then_multi"
            else ("configured_multi_worker_baseline", "single_worker_reference")
        )
        measured = {
            name: self.run_measurement_phase(name, workers=measurement_cells[name])
            for name in ordered_names
        }
        one_worker = measured["single_worker_reference"]
        scaled = measured["configured_multi_worker_baseline"]
        self.scale_workers(self.worker_count)
        if getattr(self, "formal_slo_v2", False):
            warmed_burst = self.warmed_pause_burst()
            cold_burst = self.cold_start_burst()
        else:
            warmed_burst = None
            cold_burst = self.bounded_queue_burst()
        fairness = self.model_fairness_scenario()
        self.lease_expiry_fault()
        self.redis_outage_fault()
        duplicate_run_id = scaled["run_ids"][0]
        self.duplicate_delivery_fault(duplicate_run_id)
        reconciliation = self.governance_reconciliation()
        self.evidence["reconciliation"] = {
            "database": reconciliation,
            "queue": self.queue_pressure(),
            "task_metrics": self.task_metrics(),
            "workers": self.wait_service_healthy("worker", count=self.worker_count, timeout=90),
        }
        comparison = {
            "single_worker_questions_per_second": one_worker["throughput"]["questions_per_second"],
            "multi_worker_questions_per_second": scaled["throughput"]["questions_per_second"],
            "cross_model_low_volume_claim_before_high_backlog_drained": fairness[
                "ordering_evidence"
            ]["low_claim_before_high_backlog_drained"],
            "interpretation": "observed values only; no pass/fail scaling ratio is asserted",
        }
        if warmed_burst is None:
            comparison["bounded_burst_questions_per_second"] = cold_burst["throughput"][
                "questions_per_second"
            ]
        else:
            comparison["warmed_pause_burst_questions_per_second"] = warmed_burst["throughput"][
                "questions_per_second"
            ]
            comparison["cold_start_burst_questions_per_second"] = cold_burst["throughput"][
                "questions_per_second"
            ]
        self.evidence["comparison"] = comparison
        self.write_evidence()


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--qualification-profile",
        choices=QUALIFICATION_PROFILES,
        default=DEFAULT_QUALIFICATION_PROFILE,
        help="select the default single-burst workload or the inseparable formal v2 profile",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--runs-per-phase", type=int, default=4)
    parser.add_argument("--backlog-limit", type=int, default=4)
    parser.add_argument("--burst-runs", type=int, default=6)
    parser.add_argument("--submit-concurrency", type=int, default=6)
    parser.add_argument("--run-concurrency", type=int, default=1)
    parser.add_argument("--question-quantum", type=int, default=5)
    parser.add_argument("--mock-delay-seconds", type=float, default=0.08)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--lease-seconds", type=float, default=6)
    parser.add_argument("--heartbeat-seconds", type=float, default=2)
    parser.add_argument("--worker-poll-seconds", type=float, default=0.15)
    parser.add_argument("--worker-max-attempts", type=int, default=WORKER_MAX_ATTEMPTS)
    parser.add_argument(
        "--retry-backoff-base-seconds",
        type=float,
        default=WORKER_RETRY_BACKOFF_BASE_SECONDS,
    )
    parser.add_argument(
        "--retry-backoff-cap-seconds",
        type=float,
        default=WORKER_RETRY_BACKOFF_CAP_SECONDS,
    )
    parser.add_argument(
        "--worker-shutdown-grace-seconds",
        type=float,
        default=WORKER_SHUTDOWN_GRACE_SECONDS,
    )
    parser.add_argument(
        "--redis-block-milliseconds",
        type=int,
        default=REDIS_BLOCK_MILLISECONDS,
    )
    parser.add_argument(
        "--redis-operation-timeout-seconds",
        type=float,
        default=REDIS_OPERATION_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--measurement-order",
        choices=("single_then_multi", "multi_then_single"),
        default="single_then_multi",
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=DEFAULT_ARTIFACTS_ROOT,
        help="gitignored repository-relative evidence root",
    )
    parser.add_argument(
        "--self-check-only",
        action="store_true",
        help="validate arguments, isolation, and Compose without creating containers",
    )
    args = parser.parse_args(argv)
    validate_arguments(args)
    return args


def main(argv: Sequence[str]) -> int:
    try:
        args = parse_arguments(argv)
    except ValueError as exc:
        print(f"Invalid capacity configuration: {exc}", file=sys.stderr)
        return 2
    repository_root = Path(__file__).resolve().parents[1]
    harness = Phase2Capacity(repository_root, args)
    exit_code = 1

    def interrupt(_signum: int, _frame: Any) -> None:
        raise AcceptanceInterrupted("received termination signal")

    previous_term = signal.signal(signal.SIGTERM, interrupt)
    try:
        review = harness.self_review()
        if args.self_check_only:
            print(json.dumps(sanitize(review), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        harness.write_evidence()
        print(f"Phase 2 capacity evidence: {harness.evidence_path}")
        harness.run_all()
        harness.evidence["status"] = "passed"
        exit_code = 0
    except (AcceptanceFailure, AcceptanceInterrupted, KeyboardInterrupt) as exc:
        harness.evidence["status"] = "failed"
        harness.evidence["failure"] = {
            "type": type(exc).__name__,
            "message": redact_text(str(exc)),
            "traceback": redact_text(traceback.format_exc()),
        }
        print(f"Phase 2 capacity failed: {redact_text(str(exc))}", file=sys.stderr)
    except BaseException as exc:
        harness.evidence["status"] = "failed"
        harness.evidence["failure"] = {
            "type": type(exc).__name__,
            "message": redact_text(str(exc)),
            "traceback": redact_text(traceback.format_exc()),
        }
        print(f"Unexpected capacity failure: {redact_text(str(exc))}", file=sys.stderr)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        if not args.self_check_only:
            try:
                harness.collect_diagnostics()
            except BaseException as exc:
                harness.evidence["diagnostics_error"] = redact_text(str(exc))
                if exit_code == 0:
                    harness.evidence["status"] = "failed"
                    exit_code = 1
            try:
                cleanup_error = harness.cleanup()
            except BaseException as exc:
                cleanup_error = f"cleanup raised {type(exc).__name__}: {redact_text(str(exc))}"
            if cleanup_error is not None:
                harness.evidence["status"] = "failed"
                harness.evidence["cleanup_error"] = cleanup_error
                exit_code = 1
            harness.evidence["finished_at"] = utc_now()
            try:
                harness.write_evidence()
            except BaseException as exc:
                print(f"Could not write capacity evidence: {exc}", file=sys.stderr)
                exit_code = 1

    if not args.self_check_only:
        print("Phase 2 capacity status: {}".format(harness.evidence["status"]))
        print(f"Evidence retained at: {harness.evidence_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
