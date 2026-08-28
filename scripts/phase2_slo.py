#!/usr/bin/env python3
"""Qualify the fixed Phase 2 single-host control-plane SLO profile.

The wrapper runs one warm-up and five or more isolated ``phase2_capacity``
children, validates their sanitized evidence instead of trusting an exit code,
and evaluates the preregistered ADR-0012 objectives. It is intentionally
Mock-only and dependency-free. Results apply only to the exact clean commit,
recorded host, and ``P2-local-control-plane-v1`` profile; they are not a real
Provider benchmark, production SLA, HA proof, or scaling extrapolation.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import random
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from phase2_acceptance import parse_datetime, redact_text, sanitize, utc_now  # noqa: E402

EVIDENCE_SCHEMA = "llmbenchlab-phase2-slo-evidence-v1"
CAPACITY_EVIDENCE_SCHEMA = "llmbenchlab-phase2-capacity-evidence-v1"
PROFILE_NAME = "P2-local-control-plane-v1"
DEFAULT_ARTIFACTS_ROOT = Path(".pytest_cache/artifacts/phase2-slo")
DEFAULT_MEASURED_TRIALS = 5
DEFAULT_SEED = 20260828
DEFAULT_TRIAL_TIMEOUT_SECONDS = 900
MIN_MEASURED_TRIALS = 5
MAX_MEASURED_TRIALS = 10
MIN_TRIAL_TIMEOUT_SECONDS = 300
MAX_TRIAL_TIMEOUT_SECONDS = 3600
MAX_CHILD_EVIDENCE_BYTES = 16 * 1024 * 1024
CHILD_CLEANUP_GRACE_SECONDS = 420
WARMUP_TRIALS = 1

MEASUREMENT_NAMES = (
    "single_worker_reference",
    "configured_multi_worker_baseline",
    "bounded_queue_burst_and_drain",
)
MEASUREMENT_ORDERS = {
    "single_then_multi": MEASUREMENT_NAMES,
    "multi_then_single": (
        "configured_multi_worker_baseline",
        "single_worker_reference",
        "bounded_queue_burst_and_drain",
    ),
}
FAULT_NAMES = (
    "lease_owner_sigkill_and_expiry_recovery",
    "redis_stop_start_database_reconciliation",
    "duplicate_terminal_delivery_noop",
)
PROVIDER_CREDENTIAL_ENV_KEYS = (
    "OPENAI_API_KEY",
    "LLMBENCHLAB_DEMO_API_KEY",
    "LLMBENCHLAB_REAL_API_KEY",
    "TEST_PROVIDER_KEY",
)
GIT_OVERRIDE_ENV_KEYS = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_QUARANTINE_PATH",
        "GIT_WORK_TREE",
    }
)
CHILD_ENV_ALLOWLIST = frozenset(
    {
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TMP",
        "TMPDIR",
        "TEMP",
        "USER",
        "XDG_RUNTIME_DIR",
    }
)

EXPECTED_CONFIGURATION: dict[str, int | float] = {
    "workers": 2,
    "runs_per_measurement_phase": 4,
    "backlog_limit": 4,
    "concurrent_backlog_submissions": 6,
    "submit_concurrency": 6,
    "run_concurrency": 1,
    "question_quantum": 5,
    "questions_per_run": 15,
    "run_max_tokens": 64,
    "run_input_token_reservation": 256,
    "mock_generation_delay_seconds": 0.08,
    "timeout_seconds": 180,
    "lease_seconds": 30,
    "heartbeat_seconds": 10,
    "worker_poll_seconds": 1,
    "worker_max_attempts": 3,
    "retry_backoff_base_seconds": 1.0,
    "retry_backoff_cap_seconds": 30.0,
    "worker_shutdown_grace_seconds": 30.0,
    "redis_block_milliseconds": 1000,
    "redis_operation_timeout_seconds": 1.0,
    "database_pool_size": 5,
    "database_max_overflow": 5,
    "database_pool_timeout_seconds": 2.0,
    "readiness_database_timeout_seconds": 2.0,
}
EXPECTED_DEMO = {
    "slug": "demo-general",
    "version": "1.0.0",
    "schema_version": "llmbenchlab-dataset-v1",
    "dataset_hash": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe",
    "question_count": 15,
}

EXPECTED_POLICY_LIMITS: dict[str, int | str] = {
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
    "run_lifetime_request_budget": 100,
    "run_lifetime_token_budget": 100_000,
    "run_lifetime_cost_budget_usd": "100.00000000",
    "backlog_limit": 4,
    "question_quantum": 5,
}

SLO_THRESHOLDS: dict[str, Any] = {
    "throughput": {
        "single_worker_reference": {"lcb_questions_per_second": 5.0, "max_cv": 0.15},
        "configured_multi_worker_baseline": {
            "lcb_questions_per_second": 10.0,
            "max_cv": 0.15,
        },
        "bounded_queue_burst_and_drain": {
            "lcb_questions_per_second": 6.0,
            "max_cv": 0.20,
        },
    },
    "multi_to_single_throughput_ratio": {
        "one_sided_95_lcb": 1.50,
        "max_cv": 0.15,
    },
    "latency_p95_seconds": {
        "single_worker_reference": {"queue": 3.0, "execution": 8.0, "end_to_end": 10.0},
        "configured_multi_worker_baseline": {
            "queue": 2.0,
            "execution": 5.0,
            "end_to_end": 7.0,
        },
        "bounded_queue_burst_and_drain": {
            "queue": 3.0,
            "execution": 5.0,
            "end_to_end": 8.0,
        },
    },
    "backlog_drain_seconds": 10.0,
    "lease_kill_fence_to_reclaim_seconds": 38.0,
    "lease_expiry_to_reclaim_seconds": 6.0,
    "redis_run_created_to_claim_seconds": 3.0,
}

# Student-t critical values for the suite's supported n=3..10 sample sizes.
# The two-sided table uses alpha/2=.025; the one-sided table uses alpha=.05.
TWO_SIDED_95_T_CRITICAL = {
    2: 4.3026527299,
    3: 3.1824463053,
    4: 2.7764451052,
    5: 2.5705818356,
    6: 2.4469118488,
    7: 2.3646242510,
    8: 2.3060041352,
    9: 2.2621571628,
}
ONE_SIDED_95_T_CRITICAL = {
    2: 2.9199855804,
    3: 2.3533634348,
    4: 2.1318467813,
    5: 2.0150483733,
    6: 1.9431802804,
    7: 1.8945786051,
    8: 1.8595480375,
    9: 1.8331129327,
}

ZERO_GAUGE_FIELDS = (
    "active_cancellation_requests",
    "active_provider_attempts",
    "dead_lettered",
    "due_pending",
    "expired_running",
    "governance_delayed",
    "governance_exhausted",
    "managed_backlog",
    "overdrawn_governance_scopes",
    "pending",
    "retry_scheduled",
    "running",
)
ZERO_RECONCILIATION_FIELDS = (
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
    "question_error_count",
    "missing_scope_projection_rows",
    "extra_scope_projection_rows",
    "scope_projection_field_drift",
    "missing_minute_projection_rows",
    "extra_minute_projection_rows",
    "minute_projection_field_drift",
)


class QualificationFailure(RuntimeError):
    """Raised when a suite input or preregistered qualification invariant fails."""

    evidence_path: Path | None = None


class QualificationInterrupted(QualificationFailure):
    """Raised after SIGTERM has been forwarded and the child had cleanup time."""


def require(condition: bool, message: str) -> None:
    """Raise a stable qualification error without copying untrusted child detail."""

    if not condition:
        raise QualificationFailure(message)


def _finite_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    require(
        not isinstance(value, bool) and isinstance(value, (int, float)), f"{name} is not numeric"
    )
    numeric = float(value)
    require(math.isfinite(numeric), f"{name} is not finite")
    if minimum is not None:
        require(numeric >= minimum, f"{name} is below {minimum}")
    return numeric


def _exact_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    require(not isinstance(value, bool) and isinstance(value, int), f"{name} is not an integer")
    if minimum is not None:
        require(value >= minimum, f"{name} is below {minimum}")
    return value


def _utc_elapsed_seconds(start: Any, end: Any, name: str) -> float:
    """Recompute a non-negative duration from two typed UTC child facts."""

    require(isinstance(start, str) and isinstance(end, str), f"{name} timestamps are missing")
    try:
        elapsed = (parse_datetime(end) - parse_datetime(start)).total_seconds()
    except (TypeError, ValueError) as exc:
        raise QualificationFailure(f"{name} timestamps are invalid") from exc
    require(math.isfinite(elapsed) and elapsed >= 0, f"{name} duration is negative")
    return elapsed


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    require(isinstance(value, dict), f"{name} is not an object")
    return value


def _sequence(value: Any, name: str) -> Sequence[Any]:
    require(isinstance(value, list), f"{name} is not an array")
    return value


def finite_samples(
    values: Sequence[Any],
    name: str = "samples",
    minimum: int = 3,
) -> list[float]:
    """Return a validated finite, non-boolean statistical sample."""

    require(isinstance(values, (list, tuple)), f"{name} is not a sample sequence")
    require(len(values) >= minimum, f"{name} requires at least {minimum} observations")
    return [_finite_number(value, f"{name}[{index}]") for index, value in enumerate(values)]


def percentile(values: Sequence[Any], percentage: float) -> float:
    """Return a linearly interpolated percentile for a finite non-empty sample."""

    numeric = finite_samples(values, minimum=1)
    require(0 <= percentage <= 100, "percentile must be between 0 and 100")
    ordered = sorted(numeric)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentage / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def descriptive_distribution(values: Sequence[Any]) -> dict[str, int | float]:
    """Describe raw run-level observations; p99 is descriptive only."""

    numeric = finite_samples(values, minimum=1)
    return {
        "n": len(numeric),
        "mean": statistics.fmean(numeric),
        "median": statistics.median(numeric),
        "min": min(numeric),
        "max": max(numeric),
        "p50": percentile(numeric, 50),
        "p95": percentile(numeric, 95),
        "p99": percentile(numeric, 99),
    }


def sample_statistics(values: Sequence[Any]) -> dict[str, Any]:
    """Return preregistered Student-t statistics for one trial-level sample."""

    numeric = finite_samples(values)
    degrees_of_freedom = len(numeric) - 1
    require(
        degrees_of_freedom in TWO_SIDED_95_T_CRITICAL,
        "sample size is outside the fixed Student-t table",
    )
    mean = statistics.fmean(numeric)
    sample_std = statistics.stdev(numeric)
    require(mean > 0, "qualification samples must have a positive mean")
    standard_error = sample_std / math.sqrt(len(numeric))
    two_sided_margin = TWO_SIDED_95_T_CRITICAL[degrees_of_freedom] * standard_error
    one_sided_margin = ONE_SIDED_95_T_CRITICAL[degrees_of_freedom] * standard_error
    return {
        "n": len(numeric),
        "samples": numeric,
        "mean": mean,
        "median": statistics.median(numeric),
        "min": min(numeric),
        "max": max(numeric),
        "sample_std": sample_std,
        "cv": sample_std / mean,
        "two_sided_95_ci": [mean - two_sided_margin, mean + two_sided_margin],
        "one_sided_95_lcb": mean - one_sided_margin,
        "one_sided_95_ucb": mean + one_sided_margin,
        "p99_descriptive": percentile(numeric, 99),
    }


def zero_event_upper_bound(total: int, confidence: float = 0.95) -> float:
    """Return the exact one-sided binomial upper bound when zero events occur."""

    require(
        not isinstance(total, bool) and isinstance(total, int) and total > 0, "total must be > 0"
    )
    require(
        not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and math.isfinite(float(confidence))
        and 0 < float(confidence) < 1,
        "confidence must be finite and between 0 and 1",
    )
    return 1.0 - (1.0 - float(confidence)) ** (1.0 / total)


def balanced_measurement_orders(seed: int, measured_trials: int) -> list[str]:
    """Generate a deterministic, count-balanced order plan before measurements."""

    require(not isinstance(seed, bool) and isinstance(seed, int), "seed must be an integer")
    require(
        MIN_MEASURED_TRIALS <= measured_trials <= MAX_MEASURED_TRIALS,
        f"measured trials must be between {MIN_MEASURED_TRIALS} and {MAX_MEASURED_TRIALS}",
    )
    first_count = (measured_trials + 1) // 2
    orders = ["single_then_multi"] * first_count
    orders.extend(["multi_then_single"] * (measured_trials - first_count))
    random.Random(seed).shuffle(orders)
    return orders


def normalized_fingerprint(value: Any) -> str:
    """Return a canonical SHA-256 for a normalized JSON-compatible value."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise QualificationFailure("value could not be canonically fingerprinted") from exc
    return hashlib.sha256(encoded).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QualificationFailure("child evidence contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise QualificationFailure("child evidence contains NaN or Infinity")


def _safe_repository_file(path: Path, repository_root: Path) -> Path:
    root = repository_root.resolve(strict=True)
    lexical = path if path.is_absolute() else root / path
    lexical = Path(os.path.abspath(lexical))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise QualificationFailure("child evidence path escaped the repository") from exc

    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise QualificationFailure("child evidence path contains a symlink")
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise QualificationFailure("child evidence is missing or escaped the repository") from exc
    try:
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise QualificationFailure("child evidence could not be inspected") from exc
    require(stat.S_ISREG(mode), "child evidence is not a regular file")
    return resolved


def strict_json_load(
    path: Path,
    repository_root: Path,
    max_bytes: int = MAX_CHILD_EVIDENCE_BYTES,
) -> dict[str, Any]:
    """Read bounded repository-internal JSON, rejecting symlinks and extensions."""

    require(
        not isinstance(max_bytes, bool) and isinstance(max_bytes, int) and max_bytes > 0,
        "max_bytes must be a positive integer",
    )
    _safe_path, raw_bytes = _read_repository_file(path, repository_root, max_bytes=max_bytes)
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QualificationFailure("child evidence is not readable UTF-8") from exc
    return _parse_strict_json(raw)


def _parse_strict_json(raw: str) -> dict[str, Any]:
    """Parse one bounded UTF-8 JSON document under the evidence contract."""

    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except QualificationFailure:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise QualificationFailure("child evidence is not strict JSON") from exc
    require(isinstance(parsed, dict), "child evidence root is not an object")
    return parsed


def _read_repository_file(
    path: Path,
    repository_root: Path,
    *,
    max_bytes: int,
) -> tuple[Path, bytes]:
    """Read one regular repository file once without following a final symlink."""

    safe_path = _safe_repository_file(path, repository_root)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(safe_path, flags)
    except OSError as exc:
        raise QualificationFailure("child evidence could not be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        require(stat.S_ISREG(metadata.st_mode), "child evidence is not a regular file")
        require(
            0 < metadata.st_size <= max_bytes,
            "child evidence size is outside the allowed range",
        )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        require(0 < len(raw) <= max_bytes, "child evidence size changed while reading")
        require(len(raw) == metadata.st_size, "child evidence changed while reading")
        return safe_path, raw
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_environment(environment: Mapping[str, Any]) -> dict[str, Any]:
    host = _mapping(environment.get("host"), "environment.host")
    docker = _mapping(environment.get("docker"), "environment.docker")
    host_cpu = _exact_int(host.get("logical_cpu_count"), "host logical CPU", minimum=8)
    host_memory = _exact_int(host.get("memory_bytes"), "host memory", minimum=8_000_000_000)
    docker_cpu = _exact_int(docker.get("logical_cpu_count"), "Docker logical CPU", minimum=8)
    docker_memory = _exact_int(docker.get("memory_bytes"), "Docker memory", minimum=4_000_000_000)
    postgres_version = str(environment.get("postgres_version") or "")
    redis_version = str(environment.get("redis_version") or "")
    require(postgres_version.startswith("16."), "PostgreSQL major version is not 16")
    require(redis_version.startswith("7."), "Redis major version is not 7")
    max_connections = _exact_int(
        environment.get("postgres_max_connections"),
        "PostgreSQL max_connections",
        minimum=100,
    )

    normalized_limits: dict[str, list[dict[str, Any]]] = {}
    limits = _mapping(environment.get("container_limits"), "environment.container_limits")
    expected_counts = {"postgres": 1, "redis": 1, "api": 1, "workers": 2}
    resource_keys = (
        "service",
        "image_content_sha256",
        "memory_limit_bytes",
        "memory_swap_limit_bytes",
        "nano_cpus",
        "cpu_quota",
        "cpu_period",
        "pids_limit",
    )
    for service, expected_count in expected_counts.items():
        entries = _sequence(limits.get(service), f"container limits for {service}")
        require(len(entries) == expected_count, f"unexpected {service} container count")
        normalized_entries = []
        for entry in entries:
            item = _mapping(entry, f"{service} container limit")
            expected_service_label = "worker" if service == "workers" else service
            require(item.get("service") == expected_service_label, f"wrong {service} service label")
            image_id = str(item.get("image_id") or "")
            require(
                image_id.startswith("sha256:")
                and len(image_id) == 71
                and all(character in "0123456789abcdef" for character in image_id[7:]),
                f"{service} image identity is missing",
            )
            image_content = str(item.get("image_content_sha256") or "")
            require(
                len(image_content) == 64
                and all(character in "0123456789abcdef" for character in image_content),
                f"{service} image content identity is missing",
            )
            normalized_entries.append({key: item.get(key) for key in resource_keys})
        normalized_limits[service] = sorted(
            normalized_entries,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )

    host_fields = (
        "operating_system",
        "os_release",
        "architecture",
        "cpu_model",
        "logical_cpu_count",
        "memory_bytes",
        "python_version",
    )
    docker_fields = (
        "server_version",
        "operating_system",
        "architecture",
        "logical_cpu_count",
        "memory_bytes",
        "rootless",
    )
    runtime = _mapping(environment.get("runtime_settings"), "environment.runtime_settings")
    expected_runtime = {
        "database_pool_size": EXPECTED_CONFIGURATION["database_pool_size"],
        "database_max_overflow": EXPECTED_CONFIGURATION["database_max_overflow"],
        "database_pool_timeout_seconds": EXPECTED_CONFIGURATION["database_pool_timeout_seconds"],
        "readiness_database_timeout_seconds": EXPECTED_CONFIGURATION[
            "readiness_database_timeout_seconds"
        ],
        "worker_lease_seconds": EXPECTED_CONFIGURATION["lease_seconds"],
        "worker_heartbeat_seconds": EXPECTED_CONFIGURATION["heartbeat_seconds"],
        "worker_poll_seconds": EXPECTED_CONFIGURATION["worker_poll_seconds"],
        "worker_max_attempts": EXPECTED_CONFIGURATION["worker_max_attempts"],
        "worker_retry_backoff_base_seconds": EXPECTED_CONFIGURATION["retry_backoff_base_seconds"],
        "worker_retry_backoff_cap_seconds": EXPECTED_CONFIGURATION["retry_backoff_cap_seconds"],
        "worker_shutdown_grace_seconds": EXPECTED_CONFIGURATION["worker_shutdown_grace_seconds"],
        "redis_block_milliseconds": EXPECTED_CONFIGURATION["redis_block_milliseconds"],
        "redis_operation_timeout_seconds": EXPECTED_CONFIGURATION[
            "redis_operation_timeout_seconds"
        ],
    }
    require(set(runtime) == set(expected_runtime), "runtime Settings field set drift")
    for field, expected in expected_runtime.items():
        actual = runtime.get(field)
        if isinstance(expected, float):
            require(
                math.isclose(_finite_number(actual, f"runtime.{field}"), expected, abs_tol=1e-12),
                f"runtime.{field} drift",
            )
        else:
            require(actual == expected and not isinstance(actual, bool), f"runtime.{field} drift")

    return {
        "host": {key: host.get(key) for key in host_fields}
        | {
            "logical_cpu_count": host_cpu,
            "memory_bytes": host_memory,
        },
        "docker": {key: docker.get(key) for key in docker_fields}
        | {
            "logical_cpu_count": docker_cpu,
            "memory_bytes": docker_memory,
        },
        "postgres_version": postgres_version,
        "redis_version": redis_version,
        "postgres_max_connections": max_connections,
        "container_limits": normalized_limits,
        "runtime_settings": dict(runtime),
    }


def _normalize_data(data: Mapping[str, Any]) -> dict[str, Any]:
    require(data.get("demo_only") is True, "child data is not marked Demo-only")
    require(data.get("protocol_version") == "llmbenchlab-protocol-v1", "protocol drift")
    benchmark = _mapping(data.get("benchmark"), "data.benchmark")
    for field, expected in EXPECTED_DEMO.items():
        require(benchmark.get(field) == expected, f"Demo benchmark {field} drift")
    dataset_hash = str(benchmark.get("dataset_hash") or "")
    require(
        len(dataset_hash) == 64
        and all(character in "0123456789abcdef" for character in dataset_hash),
        "Demo dataset hash is invalid",
    )

    models = (
        _mapping(data.get("model"), "data.model"),
        _mapping(data.get("fairness_low_volume_model"), "data.fairness_low_volume_model"),
    )
    for model in models:
        require(model.get("provider_type") == "mock", "child model is not Mock")
        require(model.get("enabled") is True, "child Mock model is disabled")
        require(model.get("api_key_env") is None, "child Mock model references a Provider key")
        require(model.get("base_url") in (None, ""), "child Mock model has a custom base URL")

    policy = _mapping(data.get("governance_policy"), "data.governance_policy")
    require(policy.get("is_active") is True, "governance policy is not active")
    require(policy.get("limits") == EXPECTED_POLICY_LIMITS, "governance policy drift")
    return {
        "benchmark": {
            "slug": EXPECTED_DEMO["slug"],
            "dataset_hash": dataset_hash,
            "question_count": EXPECTED_DEMO["question_count"],
            "version": EXPECTED_DEMO["version"],
            "schema_version": EXPECTED_DEMO["schema_version"],
        },
        "models": [
            {"provider_type": "mock", "enabled": True, "api_key_env": None},
            {"provider_type": "mock", "enabled": True, "api_key_env": None},
        ],
        "protocol_version": "llmbenchlab-protocol-v1",
        "demo_only": True,
        "governance_policy": {
            "is_active": True,
            "limits": EXPECTED_POLICY_LIMITS,
        },
    }


def _validate_topology(topology: Mapping[str, Any]) -> None:
    for service, expected_count in {"postgres": 1, "redis": 1, "api": 1, "workers": 2}.items():
        entries = _sequence(topology.get(service), f"topology.{service}")
        require(len(entries) == expected_count, f"unexpected topology count for {service}")
        for entry in entries:
            item = _mapping(entry, f"topology.{service} entry")
            require(item.get("status") == "running", f"{service} was not running")
            require(item.get("health") == "healthy", f"{service} was not healthy")
    ready = _mapping(topology.get("ready"), "topology.ready")
    require(
        ready.get("status") == "ready"
        and ready.get("database") == "ok"
        and ready.get("queue") == "ok"
        and ready.get("schema") == "ok",
        "initial readiness was not healthy",
    )


def _validate_zero_gauges(value: Any, name: str) -> None:
    gauges = _mapping(value, name)
    for field in ZERO_GAUGE_FIELDS:
        require(_exact_int(gauges.get(field), f"{name}.{field}") == 0, f"{name}.{field} drift")


def _validate_distribution(distribution: Any, name: str, expected_count: int) -> dict[str, Any]:
    summary = _mapping(distribution, name)
    samples = finite_samples(_sequence(summary.get("samples"), f"{name}.samples"), name, minimum=1)
    require(len(samples) == expected_count, f"{name} has the wrong raw sample count")
    require(all(value >= 0 for value in samples), f"{name} contains a negative latency")
    observed_count = _exact_int(summary.get("count"), f"{name}.count")
    require(observed_count == expected_count, f"{name}.count disagrees with raw samples")
    expected_p95 = percentile(samples, 95)
    expected_p99 = percentile(samples, 99)
    observed_p95 = _finite_number(summary.get("p95"), f"{name}.p95", minimum=0)
    observed_p99 = _finite_number(summary.get("p99"), f"{name}.p99", minimum=0)
    require(math.isclose(observed_p95, expected_p95, abs_tol=1e-6), f"{name}.p95 drift")
    require(math.isclose(observed_p99, expected_p99, abs_tol=1e-6), f"{name}.p99 drift")
    return {"samples": samples, "p95": observed_p95, "p99": observed_p99}


def _validate_measurement(measurement: Mapping[str, Any], expected_name: str) -> dict[str, Any]:
    require(measurement.get("name") == expected_name, "measurement name/order drift")
    expected_workers = 1 if expected_name == "single_worker_reference" else 2
    require(
        _exact_int(measurement.get("workers"), f"{expected_name}.workers") == expected_workers,
        f"{expected_name} worker count drift",
    )
    submission = _mapping(measurement.get("submission"), f"{expected_name}.submission")
    expected_requested = 6 if expected_name == "bounded_queue_burst_and_drain" else 4
    require(
        _exact_int(submission.get("requested"), "submission.requested") == expected_requested,
        "submission count drift",
    )
    require(
        _exact_int(submission.get("accepted"), "submission.accepted") == 4, "accepted count drift"
    )
    expected_rejected = 2 if expected_requested == 6 else 0
    require(
        _exact_int(submission.get("rejected"), "submission.rejected") == expected_rejected,
        "rejected count drift",
    )
    expected_statuses = {"202": 4, "429": 2} if expected_rejected else {"202": 4}
    require(submission.get("status_counts") == expected_statuses, "submission status drift")

    throughput = _mapping(measurement.get("throughput"), f"{expected_name}.throughput")
    observed_qps = _finite_number(
        throughput.get("questions_per_second"), f"{expected_name}.qps", minimum=0
    )
    require(
        _exact_int(throughput.get("completed_runs"), "completed runs") == 4, "completed Run drift"
    )
    require(
        _exact_int(throughput.get("completed_questions"), "completed questions") == 60,
        "completed question drift",
    )
    wall_duration = _finite_number(
        measurement.get("wall_duration_seconds"),
        f"{expected_name}.wall_duration_seconds",
        minimum=0,
    )
    require(wall_duration > 0, f"{expected_name} wall duration was zero")
    recomputed_qps = 60 / wall_duration
    require(
        observed_qps == round(recomputed_qps, 6),
        f"{expected_name} throughput disagrees with completed questions / wall duration",
    )
    require(
        _exact_int(measurement.get("response_count"), "response count") == 60,
        "Response count drift",
    )

    errors = _mapping(measurement.get("errors_and_retries"), f"{expected_name}.errors")
    require(errors.get("terminal_statuses") == {"completed": 4}, "terminal status drift")
    require(
        _exact_int(errors.get("question_errors"), "question errors") == 0,
        "question errors observed",
    )
    require(
        _exact_int(errors.get("failed_attempt_count"), "failed attempts") == 0,
        "unexpected failed attempt",
    )
    attempts = _mapping(measurement.get("provider_attempts"), f"{expected_name}.attempts")
    require(
        _exact_int(attempts.get("provider_reservations"), "provider reservations") == 60
        and _exact_int(attempts.get("settled_actual_reservations"), "actual reservations") == 60
        and _exact_int(
            attempts.get("settled_conservative_reservations"), "conservative reservations"
        )
        == 0,
        "measurement Provider-attempt ledger delta drift",
    )
    require(
        math.isclose(
            _finite_number(
                attempts.get("attempts_per_completed_question"),
                "attempts per completed question",
            ),
            1.0,
            rel_tol=0,
            abs_tol=1e-12,
        ),
        "measurement Provider attempts/question drift",
    )

    scheduling = _mapping(measurement.get("cooperative_scheduling"), "cooperative scheduling")
    require(
        scheduling.get("all_runs_dispatched_more_than_once") is True, "Run did not receive slices"
    )
    require(scheduling.get("all_runs_yielded") is True, "Run did not cooperatively yield")
    per_run = _sequence(scheduling.get("per_run"), "cooperative scheduling per_run")
    require(len(per_run) == 4, "cooperative scheduling Run count drift")
    for run in per_run:
        item = _mapping(run, "cooperative scheduling Run")
        require(
            _exact_int(item.get("dispatch_count"), "dispatch count") >= 3,
            "Run dispatch count too low",
        )
        require(
            _exact_int(item.get("cooperative_yield_events"), "yield count") >= 2,
            "Run yield count too low",
        )

    latency = _mapping(measurement.get("latency_seconds"), f"{expected_name}.latency")
    latency_metrics = {
        dimension: _validate_distribution(
            latency.get(dimension), f"{expected_name}.{dimension}", expected_count=4
        )
        for dimension in ("queue", "execution", "end_to_end")
    }
    question_latency = _validate_distribution(
        measurement.get("question_latency_ms"),
        f"{expected_name}.question_latency_ms",
        expected_count=60,
    )
    database = _mapping(measurement.get("database"), f"{expected_name}.database")
    task_metrics = _mapping(database.get("task_metrics"), f"{expected_name}.task_metrics")
    _validate_zero_gauges(task_metrics.get("final_database_gauges"), f"{expected_name}.gauges")
    queue = _mapping(measurement.get("queue"), f"{expected_name}.queue")
    queue_after = _mapping(queue.get("after"), f"{expected_name}.queue.after")
    group = _mapping(queue_after.get("group"), f"{expected_name}.queue.after.group")
    require(_exact_int(group.get("pending"), "queue pending") == 0, "Redis PEL did not drain")
    require(_exact_int(group.get("lag"), "queue lag") == 0, "Redis lag did not drain")

    result: dict[str, Any] = {
        "questions_per_second": recomputed_qps,
        "wall_duration_seconds": wall_duration,
        "latency_seconds": latency_metrics,
        "question_latency_ms": question_latency,
        "completed_runs": 4,
        "completed_questions": 60,
        "question_errors": 0,
        "provider_attempts_per_question": 1.0,
        "all_runs_yielded": True,
    }
    if expected_name == "bounded_queue_burst_and_drain":
        require(
            measurement.get("observed_status_counts") == expected_statuses, "burst status drift"
        )
        typed = _sequence(measurement.get("typed_rejections"), "typed backlog rejections")
        require(len(typed) == 2, "typed backlog rejection count drift")
        for rejection in typed:
            item = _mapping(rejection, "typed backlog rejection")
            detail = _mapping(
                _mapping(item.get("payload"), "rejection payload").get("detail"), "rejection detail"
            )
            require(
                item.get("status_code") == 429
                and detail.get("code") == "run_backlog_full"
                and detail.get("limit") == 4,
                "backlog rejection was not typed",
            )
        require(measurement.get("accepted_runs_preserved") is True, "accepted backlog Run was lost")
        _validate_zero_gauges(measurement.get("backlog_after_drain"), "backlog after drain")
        result["backlog_drain_seconds"] = _finite_number(
            measurement.get("backlog_drain_seconds"), "backlog drain seconds", minimum=0
        )
        result["admission_status_counts"] = expected_statuses
    return result


def _validate_fairness(value: Any) -> dict[str, bool]:
    fairness = _mapping(value, "fairness")
    require(fairness.get("name") == "cross_model_fair_quantum_ordering", "fairness scenario drift")
    require(fairness.get("question_quantum") == 5, "fairness quantum drift")
    require(fairness.get("configured_backlog_limit") == 4, "fairness backlog drift")
    ordering = _mapping(fairness.get("ordering_evidence"), "fairness ordering")
    require(ordering.get("low_volume_claim_observed") is True, "low-volume claim missing")
    require(ordering.get("low_volume_slice_observed") is True, "low-volume slice missing")
    require(
        ordering.get("low_claim_before_high_backlog_drained") is True, "fairness ordering failed"
    )
    require(
        _exact_int(ordering.get("high_volume_incomplete_at_low_slice"), "incomplete high Runs") > 0,
        "high-volume backlog was already drained",
    )
    terminal_runs = _sequence(fairness.get("terminal_runs"), "fairness terminal Runs")
    require(len(terminal_runs) == 4, "fairness terminal Run count drift")
    for run in terminal_runs:
        item = _mapping(run, "fairness terminal Run")
        require(item.get("status") == "completed", "fairness Run was not completed")
        require(item.get("completed_questions") == 15, "fairness Run was incomplete")
    _validate_zero_gauges(fairness.get("backlog_after_drain"), "fairness backlog after drain")
    return {
        "low_volume_claim_observed": True,
        "low_volume_slice_observed": True,
        "low_claim_before_high_backlog_drained": True,
    }


def _validate_faults(value: Any) -> dict[str, Any]:
    faults = _sequence(value, "faults")
    require(len(faults) == 3, "fault scenario count drift")
    indexed: dict[str, Mapping[str, Any]] = {}
    for fault in faults:
        item = _mapping(fault, "fault")
        name = str(item.get("name") or "")
        require(name in FAULT_NAMES and name not in indexed, "fault scenario name drift")
        indexed[name] = item
    require(set(indexed) == set(FAULT_NAMES), "fault scenario set drift")

    lease = indexed["lease_owner_sigkill_and_expiry_recovery"]
    terminal = _mapping(lease.get("terminal"), "lease terminal")
    require(
        terminal.get("status") == "completed"
        and terminal.get("completed_questions") == 15
        and terminal.get("total_questions") == 15
        and terminal.get("error_questions") == 0,
        "lease recovery Run was not complete",
    )
    require(
        _exact_int(terminal.get("attempt_count"), "lease attempts") >= 2, "lease was not reclaimed"
    )
    require(
        _exact_int(terminal.get("failed_attempt_count"), "lease failed attempts") == 1,
        "lease recovery did not record exactly one expected failed delivery",
    )
    victim = _mapping(lease.get("victim_after_kill"), "lease killed worker")
    require(
        victim.get("status") == "exited" and victim.get("exit_code") == 137,
        "Worker SIGKILL was not confirmed",
    )
    _validate_zero_gauges(
        _mapping(lease.get("task_metrics"), "lease task metrics").get("final_database_gauges"),
        "lease final gauges",
    )
    lease_timing = _mapping(lease.get("timing"), "lease timing")
    kill_fence_to_reclaim = _finite_number(
        lease_timing.get("kill_fence_to_reclaim_seconds"),
        "kill fence to reclaim",
        minimum=0,
    )
    expiry_to_reclaim = _finite_number(
        lease_timing.get("lease_expiry_to_reclaim_seconds"),
        "lease expiry to reclaim",
        minimum=0,
    )
    recomputed_kill_fence = _utc_elapsed_seconds(
        lease_timing.get("kill_fence_database_at"),
        lease_timing.get("reclaim_occurred_at"),
        "kill fence to reclaim",
    )
    recomputed_expiry = _utc_elapsed_seconds(
        lease_timing.get("old_lease_expires_at"),
        lease_timing.get("reclaim_occurred_at"),
        "lease expiry to reclaim",
    )
    require(
        math.isclose(kill_fence_to_reclaim, recomputed_kill_fence, abs_tol=1e-6),
        "kill-fence recovery duration drift",
    )
    require(
        math.isclose(expiry_to_reclaim, recomputed_expiry, abs_tol=1e-6),
        "lease-expiry recovery duration drift",
    )

    redis = indexed["redis_stop_start_database_reconciliation"]
    require(
        redis.get("pending_last_error") == "queue_notification_unavailable",
        "Redis DB-first marker missing",
    )
    require(redis.get("terminal_status") == "completed", "Redis-outage Run was not completed")
    degraded = _mapping(redis.get("ready_while_stopped"), "Redis degraded readiness")
    require(
        degraded.get("status") == "degraded"
        and degraded.get("database") == "ok"
        and degraded.get("queue") == "unavailable"
        and degraded.get("accepting_runs") is True,
        "Redis outage boundary drift",
    )
    workers = _sequence(redis.get("workers_while_redis_stopped"), "Redis-outage workers")
    require(len(workers) == 2, "Redis-outage Worker count drift")
    require(
        all(
            _mapping(worker, "Redis-outage worker").get("status") == "running"
            and _mapping(worker, "Redis-outage worker").get("health") == "healthy"
            for worker in workers
        ),
        "Workers were not healthy while Redis was stopped",
    )
    _validate_zero_gauges(
        _mapping(redis.get("task_metrics"), "Redis task metrics").get("final_database_gauges"),
        "Redis final gauges",
    )
    redis_timing = _mapping(redis.get("timing"), "Redis timing")
    created_to_claim = _finite_number(
        redis_timing.get("run_created_to_claim_seconds"),
        "Redis Run created_at to claim",
        minimum=0,
    )
    created_to_terminal = _finite_number(
        redis_timing.get("run_created_to_terminal_seconds"),
        "Redis Run created_at to terminal",
        minimum=0,
    )
    recomputed_redis_claim = _utc_elapsed_seconds(
        redis_timing.get("run_created_at"),
        redis_timing.get("first_claim_occurred_at"),
        "Redis Run created_at to claim",
    )
    recomputed_redis_terminal = _utc_elapsed_seconds(
        redis_timing.get("run_created_at"),
        redis_timing.get("terminal_at"),
        "Redis Run created_at to terminal",
    )
    require(
        math.isclose(created_to_claim, recomputed_redis_claim, abs_tol=1e-6),
        "Redis claim recovery duration drift",
    )
    require(
        math.isclose(created_to_terminal, recomputed_redis_terminal, abs_tol=1e-6),
        "Redis terminal recovery duration drift",
    )
    require(created_to_terminal >= created_to_claim, "Redis terminal preceded claim")

    duplicate = indexed["duplicate_terminal_delivery_noop"]
    snapshot_hash = str(duplicate.get("snapshot_sha256") or "")
    before_hash = str(duplicate.get("before_snapshot_sha256") or "")
    after_hash = str(duplicate.get("after_snapshot_sha256") or "")
    require(
        len(snapshot_hash) == 64
        and snapshot_hash == before_hash == after_hash
        and all(character in "0123456789abcdef" for character in snapshot_hash),
        "duplicate-delivery canonical snapshots drifted",
    )
    queue = _mapping(duplicate.get("queue_after_ack"), "duplicate queue")
    require(queue.get("pending") == 0 and queue.get("lag") == 0, "duplicate delivery left PEL/lag")
    return {
        "lease_kill_fence_to_reclaim_seconds": kill_fence_to_reclaim,
        "lease_expiry_to_reclaim_seconds": expiry_to_reclaim,
        "redis_run_created_to_claim_seconds": created_to_claim,
        "redis_run_created_to_terminal_seconds": created_to_terminal,
        "lease_recovered": True,
        "redis_completed_while_stopped": True,
        "duplicate_delivery_noop": True,
    }


def _validate_reconciliation(value: Any) -> dict[str, Any]:
    reconciliation = _mapping(value, "reconciliation")
    database = _mapping(reconciliation.get("database"), "reconciliation.database")
    for field in ZERO_RECONCILIATION_FIELDS:
        require(
            _exact_int(database.get(field), f"reconciliation.{field}") == 0,
            f"reconciliation.{field} drift",
        )
    for field, expected in {
        "policies": 2,
        "active_policies": 1,
        "runs": 18,
        "responses": 270,
        "distinct_run_question_responses": 270,
        "question_executions": 270,
        "reservations": 271,
        "failed_attempt_count": 1,
    }.items():
        require(
            _exact_int(database.get(field), f"reconciliation.{field}") == expected,
            f"reconciliation.{field} drift",
        )
    require(
        database.get("reservation_states") == {"settled_actual": 270, "settled_conservative": 1},
        "Provider attempt terminal-state counts drift",
    )
    audit_events = _exact_int(database.get("audit_events"), "reconciliation.audit_events")
    require(audit_events > 0, "audit evidence missing")
    audit_types = _mapping(database.get("audit_event_types"), "reconciliation audit types")
    for event_type in (
        "provider_attempt_reserved",
        "provider_attempt_send_started",
        "provider_attempt_settled",
    ):
        require(
            _exact_int(audit_types.get(event_type), f"audit count {event_type}") == 271,
            f"{event_type} count drift",
        )
    queue = _mapping(reconciliation.get("queue"), "reconciliation.queue")
    group = _mapping(queue.get("group"), "reconciliation.queue.group")
    require(group.get("pending") == 0 and group.get("lag") == 0, "final Redis PEL/lag drift")
    _validate_zero_gauges(reconciliation.get("task_metrics"), "reconciliation.task_metrics")
    workers = _sequence(reconciliation.get("workers"), "reconciliation.workers")
    require(len(workers) == 2, "final Worker count drift")
    require(
        all(
            _mapping(worker, "final worker").get("status") == "running"
            and _mapping(worker, "final worker").get("health") == "healthy"
            for worker in workers
        ),
        "final Worker state was unhealthy",
    )
    return {
        "runs": 18,
        "responses": 270,
        "question_executions": 270,
        "duplicate_operation_keys": 0,
        "duplicate_audit_event_keys": 0,
        "question_errors": 0,
        "active_or_reserved_residue": 0,
        "redis_pending": 0,
        "redis_lag": 0,
    }


def _validate_cleanup(value: Any) -> dict[str, Any]:
    cleanup = _mapping(value, "cleanup")
    require(cleanup.get("status") == "passed", "child cleanup did not pass")
    require(cleanup.get("down_returncode") == 0, "Compose down failed")
    for field in (
        "remaining_containers",
        "remaining_project_volumes",
        "remaining_project_networks",
    ):
        require(cleanup.get(field) == [], f"cleanup left {field}")
    return {
        "status": "passed",
        "down_returncode": 0,
        "remaining_containers": 0,
        "remaining_project_volumes": 0,
        "remaining_project_networks": 0,
    }


def validate_child_evidence(
    evidence: dict[str, Any],
    *,
    expected_commit: str,
    expected_hashes: dict[str, str],
    expected_order: str,
    expected_environment_fingerprint: str | None = None,
    expected_configuration_fingerprint: str | None = None,
    expected_data_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Validate and reduce one capacity child to an aggregate-safe allowlist."""

    require(evidence.get("schema_version") == CAPACITY_EVIDENCE_SCHEMA, "child schema drift")
    require(evidence.get("status") == "passed", "child status did not pass")
    require(evidence.get("offline_only") is True, "child is not offline-only")
    require(evidence.get("production_slo") is False, "child mislabels capacity as production SLO")
    require(evidence.get("failure") is None, "passed child retained a failure")

    repository = _mapping(evidence.get("repository"), "repository")
    require(repository.get("commit") == expected_commit, "child commit drift")
    require(repository.get("dirty") is False, "child repository was dirty")
    require(repository.get("status_paths") == [], "child repository status was not empty")
    for field in ("capacity_script_sha256", "acceptance_script_sha256", "compose_sha256"):
        require(repository.get(field) == expected_hashes.get(field), f"child {field} drift")

    configuration = _mapping(evidence.get("configuration"), "configuration")
    require(
        set(configuration) == {*EXPECTED_CONFIGURATION, "measurement_order"},
        "child configuration field set drift",
    )
    for field, expected in EXPECTED_CONFIGURATION.items():
        actual = configuration.get(field)
        if isinstance(expected, float):
            require(
                math.isclose(
                    _finite_number(actual, f"configuration.{field}"), expected, abs_tol=1e-12
                ),
                f"configuration.{field} drift",
            )
        else:
            require(
                actual == expected and not isinstance(actual, bool), f"configuration.{field} drift"
            )
    require(expected_order in MEASUREMENT_ORDERS, "wrapper requested an unknown order")
    require(configuration.get("measurement_order") == expected_order, "measurement order drift")
    normalized_configuration = dict(configuration)
    normalized_configuration.pop("measurement_order", None)
    configuration_fingerprint = normalized_fingerprint(normalized_configuration)
    if expected_configuration_fingerprint is not None:
        require(
            configuration_fingerprint == expected_configuration_fingerprint,
            "normalized child configuration changed between trials",
        )

    environment = _normalize_environment(_mapping(evidence.get("environment"), "environment"))
    environment_fingerprint = normalized_fingerprint(environment)
    if expected_environment_fingerprint is not None:
        require(
            environment_fingerprint == expected_environment_fingerprint,
            "child environment changed between trials",
        )

    data = _normalize_data(_mapping(evidence.get("data"), "data"))
    data_fingerprint = normalized_fingerprint(data)
    if expected_data_fingerprint is not None:
        require(
            data_fingerprint == expected_data_fingerprint, "Mock data/policy changed between trials"
        )

    _validate_topology(_mapping(evidence.get("topology"), "topology"))
    self_review = _mapping(evidence.get("self_review"), "self_review")
    require(self_review.get("status") == "passed", "capacity self-review did not pass")
    require(self_review.get("mock_only") is True, "capacity self-review is not Mock-only")
    require(self_review.get("finite_governance_policy") is True, "governance policy was not finite")
    removed_keys = set(
        _sequence(self_review.get("real_provider_credentials_removed"), "removed credentials")
    )
    require(
        set(PROVIDER_CREDENTIAL_ENV_KEYS).issubset(removed_keys),
        "Provider credentials were not removed",
    )

    measurements_raw = _sequence(evidence.get("measurements"), "measurements")
    require(len(measurements_raw) == 3, "measurement count drift")
    expected_names = MEASUREMENT_ORDERS[expected_order]
    require(
        [str(_mapping(item, "measurement").get("name")) for item in measurements_raw]
        == list(expected_names),
        "measurement execution order drift",
    )
    measurements: dict[str, Any] = {}
    for raw, name in zip(measurements_raw, expected_names, strict=True):
        measurements[name] = _validate_measurement(_mapping(raw, f"measurement {name}"), name)
    require(set(measurements) == set(MEASUREMENT_NAMES), "measurement names were not unique")

    fairness = _validate_fairness(evidence.get("fairness"))
    faults = _validate_faults(evidence.get("faults"))
    reconciliation = _validate_reconciliation(evidence.get("reconciliation"))
    cleanup = _validate_cleanup(evidence.get("cleanup"))
    hard_invariants = {
        "child_passed": True,
        "mock_only": True,
        "clean_exact_commit": True,
        "fixed_finite_profile": True,
        "three_unique_measurements": True,
        "all_measurement_runs_completed": True,
        "all_measurement_questions_answered": True,
        "zero_measurement_question_errors": True,
        "all_measurement_runs_yielded": True,
        "exact_typed_backlog_admission": True,
        "cross_model_fairness": True,
        "three_faults_recovered": True,
        "zero_reconciliation_drift": True,
        "redis_pel_and_lag_zero": True,
        "cleanup_empty": True,
    }
    return {
        "environment": environment,
        "environment_fingerprint": environment_fingerprint,
        "configuration_fingerprint": configuration_fingerprint,
        "data_fingerprint": data_fingerprint,
        "metrics": {
            "measurements": measurements,
            "fairness": fairness,
            "faults": faults,
            "reconciliation": reconciliation,
            "cleanup": cleanup,
        },
        "hard_invariants": hard_invariants,
    }


def evaluate_suite(measured_trials: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate preregistered performance SLOs after all hard gates pass."""

    require(
        MIN_MEASURED_TRIALS <= len(measured_trials) <= MAX_MEASURED_TRIALS,
        "measured trial count is outside the qualification contract",
    )
    for trial in measured_trials:
        invariants = _mapping(trial.get("hard_invariants"), "trial hard invariants")
        require(
            bool(invariants) and all(value is True for value in invariants.values()),
            "hard invariant failed",
        )

    throughput_statistics: dict[str, Any] = {}
    latency_statistics: dict[str, Any] = {}
    error_bounds: dict[str, Any] = {}
    slo_results: list[dict[str, Any]] = []

    for name in MEASUREMENT_NAMES:
        qps_samples = [
            _mapping(_mapping(trial["metrics"], "metrics")["measurements"], "measurements")[name][
                "questions_per_second"
            ]
            for trial in measured_trials
        ]
        qps_statistics = sample_statistics(qps_samples)
        throughput_statistics[name] = qps_statistics
        threshold = SLO_THRESHOLDS["throughput"][name]
        qps_passed = qps_statistics["one_sided_95_lcb"] >= threshold["lcb_questions_per_second"]
        cv_passed = qps_statistics["cv"] <= threshold["max_cv"]
        slo_results.append(
            {
                "name": f"{name}.throughput",
                "objective": {
                    "one_sided_95_lcb_gte": threshold["lcb_questions_per_second"],
                    "sample_cv_lte": threshold["max_cv"],
                },
                "observed": {
                    "one_sided_95_lcb": qps_statistics["one_sided_95_lcb"],
                    "sample_cv": qps_statistics["cv"],
                },
                "passed": qps_passed and cv_passed,
            }
        )

        latency_statistics[name] = {}
        for dimension in ("queue", "execution", "end_to_end"):
            per_trial_p95 = [
                trial["metrics"]["measurements"][name]["latency_seconds"][dimension]["p95"]
                for trial in measured_trials
            ]
            raw_samples = [
                sample
                for trial in measured_trials
                for sample in trial["metrics"]["measurements"][name]["latency_seconds"][dimension][
                    "samples"
                ]
            ]
            objective = SLO_THRESHOLDS["latency_p95_seconds"][name][dimension]
            latency_statistics[name][dimension] = {
                "per_trial_p95_samples": finite_samples(per_trial_p95),
                "combined_raw_run_distribution": descriptive_distribution(raw_samples),
                "combined_raw_run_samples": finite_samples(raw_samples, minimum=1),
            }
            slo_results.append(
                {
                    "name": f"{name}.{dimension}_p95",
                    "objective": {"each_trial_lte_seconds": objective},
                    "observed": {"per_trial_p95_seconds": per_trial_p95},
                    "passed": all(value <= objective for value in per_trial_p95),
                }
            )

        question_total = sum(
            trial["metrics"]["measurements"][name]["completed_questions"]
            for trial in measured_trials
        )
        question_errors = sum(
            trial["metrics"]["measurements"][name]["question_errors"] for trial in measured_trials
        )
        require(question_errors == 0, "measured question error escaped child validation")
        error_bounds[name] = {
            "observed_errors": 0,
            "observed_questions": question_total,
            "one_sided_95_zero_event_upper": zero_event_upper_bound(question_total),
        }

    paired_scaling_samples = [
        trial["metrics"]["measurements"]["configured_multi_worker_baseline"]["questions_per_second"]
        / trial["metrics"]["measurements"]["single_worker_reference"]["questions_per_second"]
        for trial in measured_trials
    ]
    scaling_statistics = sample_statistics(paired_scaling_samples)
    scaling_threshold = SLO_THRESHOLDS["multi_to_single_throughput_ratio"]
    scaling_passed = (
        scaling_statistics["one_sided_95_lcb"] >= scaling_threshold["one_sided_95_lcb"]
        and scaling_statistics["cv"] <= scaling_threshold["max_cv"]
    )
    slo_results.append(
        {
            "name": "configured_multi_worker_baseline.paired_scaling_ratio",
            "objective": {
                "one_sided_95_lcb_gte": scaling_threshold["one_sided_95_lcb"],
                "sample_cv_lte": scaling_threshold["max_cv"],
            },
            "observed": {
                "one_sided_95_lcb": scaling_statistics["one_sided_95_lcb"],
                "sample_cv": scaling_statistics["cv"],
            },
            "passed": scaling_passed,
        }
    )

    drain_samples = [
        trial["metrics"]["measurements"]["bounded_queue_burst_and_drain"]["backlog_drain_seconds"]
        for trial in measured_trials
    ]
    slo_results.append(
        {
            "name": "backlog.drain",
            "objective": {"each_trial_lte_seconds": SLO_THRESHOLDS["backlog_drain_seconds"]},
            "observed": {"samples_seconds": drain_samples},
            "passed": all(
                value <= SLO_THRESHOLDS["backlog_drain_seconds"] for value in drain_samples
            ),
        }
    )

    recovery_statistics: dict[str, list[float]] = {}
    for metric, threshold_name in (
        (
            "lease_kill_fence_to_reclaim_seconds",
            "lease_kill_fence_to_reclaim_seconds",
        ),
        ("lease_expiry_to_reclaim_seconds", "lease_expiry_to_reclaim_seconds"),
        ("redis_run_created_to_claim_seconds", "redis_run_created_to_claim_seconds"),
    ):
        samples = [trial["metrics"]["faults"][metric] for trial in measured_trials]
        recovery_statistics[metric] = finite_samples(samples)
        objective = SLO_THRESHOLDS[threshold_name]
        slo_results.append(
            {
                "name": f"recovery.{metric}",
                "objective": {"each_trial_lte_seconds": objective},
                "observed": {"samples_seconds": samples},
                "passed": all(value <= objective for value in samples),
            }
        )

    hard_gate_passed = all(
        all(value is True for value in trial["hard_invariants"].values())
        for trial in measured_trials
    )
    slo_results.append(
        {
            "name": "hard_correctness_and_cleanup",
            "objective": {"every_trial": True},
            "observed": {"every_trial": hard_gate_passed},
            "passed": hard_gate_passed,
        }
    )
    all_passed = all(result["passed"] is True for result in slo_results)
    multi_lcb = throughput_statistics["configured_multi_worker_baseline"]["one_sided_95_lcb"]
    safety_factor = 0.70
    questions_per_run = 15
    provider_attempts_per_question = statistics.fmean(
        trial["metrics"]["measurements"]["configured_multi_worker_baseline"][
            "provider_attempts_per_question"
        ]
        for trial in measured_trials
    )
    backlog = 4
    workers = 2
    poll_seconds = float(EXPECTED_CONFIGURATION["worker_poll_seconds"])
    redis_wait_seconds = max(
        float(EXPECTED_CONFIGURATION["redis_block_milliseconds"]) / 1000,
        float(EXPECTED_CONFIGURATION["redis_operation_timeout_seconds"]),
    )
    database_scan_bound_seconds = redis_wait_seconds + poll_seconds
    retry_backoff_seconds = float(EXPECTED_CONFIGURATION["retry_backoff_base_seconds"])
    mock_slice_service_budget_seconds = float(
        EXPECTED_CONFIGURATION["mock_generation_delay_seconds"]
    ) * int(EXPECTED_CONFIGURATION["question_quantum"])
    database_jitter_budget_seconds = 1.0
    first_slice_bound_seconds = (
        2 * database_scan_bound_seconds
        + math.ceil((backlog - 1) / workers) * mock_slice_service_budget_seconds
        + database_jitter_budget_seconds
    )
    expiry_claim_model_seconds = (
        2 * database_scan_bound_seconds + retry_backoff_seconds + database_jitter_budget_seconds
    )
    kill_fence_claim_model_seconds = (
        float(EXPECTED_CONFIGURATION["lease_seconds"])
        + 2 * database_scan_bound_seconds
        + retry_backoff_seconds
        + database_jitter_budget_seconds
    )
    application_connection_upper_bound = (1 + workers) * (
        int(EXPECTED_CONFIGURATION["database_pool_size"])
        + int(EXPECTED_CONFIGURATION["database_max_overflow"])
    )
    postgres_max_connections = min(
        int(trial["environment"]["postgres_max_connections"]) for trial in measured_trials
    )
    postgres_connections_after_reserve = postgres_max_connections - 20
    qualification_ready = all_passed and multi_lcb > 0
    capacity_model = {
        "status": "qualified" if qualification_ready else "not_qualified",
        "inputs": {
            "multi_worker_questions_per_second_lcb": multi_lcb,
            "safety_factor": safety_factor,
            "questions_per_run": questions_per_run,
            "provider_attempts_per_question": provider_attempts_per_question,
            "backlog_runs": backlog,
            "workers": workers,
            "worker_poll_seconds": poll_seconds,
            "redis_wait_bound_seconds": redis_wait_seconds,
            "database_scan_bound_seconds": database_scan_bound_seconds,
            "first_retry_backoff_seconds": retry_backoff_seconds,
            "mock_slice_service_budget_seconds": mock_slice_service_budget_seconds,
            "database_jitter_budget_seconds": database_jitter_budget_seconds,
        },
        "safe_question_arrival_rate_per_second": (
            safety_factor * multi_lcb if qualification_ready else None
        ),
        "safe_run_arrival_rate_per_second": (
            safety_factor * multi_lcb / (questions_per_run * provider_attempts_per_question)
            if qualification_ready
            else None
        ),
        "estimated_no_new_traffic_backlog_drain_seconds": (
            backlog * questions_per_run * provider_attempts_per_question / multi_lcb
            if qualification_ready
            else None
        ),
        "first_slice_model": {
            "estimated_upper_bound_seconds": first_slice_bound_seconds,
            "formula": "2*D_scan + ceil((B-1)/W)*S_mock_slice + delta_db",
        },
        "lease_takeover_model": {
            "expiry_to_claim_upper_bound_seconds": expiry_claim_model_seconds,
            "kill_fence_to_claim_upper_bound_seconds": kill_fence_claim_model_seconds,
            "expiry_objective_seconds": SLO_THRESHOLDS["lease_expiry_to_reclaim_seconds"],
            "kill_fence_objective_seconds": SLO_THRESHOLDS["lease_kill_fence_to_reclaim_seconds"],
            "expiry_model_within_objective": (
                expiry_claim_model_seconds <= SLO_THRESHOLDS["lease_expiry_to_reclaim_seconds"]
            ),
            "kill_fence_model_within_objective": (
                kill_fence_claim_model_seconds
                <= SLO_THRESHOLDS["lease_kill_fence_to_reclaim_seconds"]
            ),
        },
        "connection_safety": {
            "application_connection_upper_bound": application_connection_upper_bound,
            "postgres_max_connections": postgres_max_connections,
            "operational_reserve_connections": 20,
            "postgres_connections_after_operational_reserve": postgres_connections_after_reserve,
            "passed": application_connection_upper_bound <= postgres_connections_after_reserve,
        },
        "boundary": (
            "Mock-only single-host qualification; real Provider attempts, rate limits, "
            "network latency, token limits, price, HA, and larger topologies are excluded."
        ),
    }
    return {
        "status": "passed" if all_passed else "failed",
        "statistics": {
            "throughput_questions_per_second": throughput_statistics,
            "paired_multi_to_single_scaling_ratio": scaling_statistics,
            "latency_seconds": latency_statistics,
            "backlog_drain_seconds": finite_samples(drain_samples),
            "recovery_seconds": recovery_statistics,
            "question_error_zero_event_bounds": error_bounds,
        },
        "slo_results": slo_results,
        "capacity_model": capacity_model,
    }


def self_check_contract() -> dict[str, Any]:
    """Return a pure contract self-check without touching Git or Docker."""

    orders = balanced_measurement_orders(DEFAULT_SEED, DEFAULT_MEASURED_TRIALS)
    require(
        abs(orders.count("single_then_multi") - orders.count("multi_then_single")) <= 1,
        "order plan is unbalanced",
    )
    known = sample_statistics([5.0, 6.0, 7.0, 8.0, 9.0])
    require(known["n"] == 5 and known["sample_std"] > 0, "statistics self-check failed")
    upper = zero_event_upper_bound(300)
    require(0.009 < upper < 0.011, "zero-event bound self-check failed")
    return {
        "status": "passed",
        "schema_version": EVIDENCE_SCHEMA,
        "child_schema_version": CAPACITY_EVIDENCE_SCHEMA,
        "profile": PROFILE_NAME,
        "warmup_trials": WARMUP_TRIALS,
        "default_measured_trials": DEFAULT_MEASURED_TRIALS,
        "measured_trial_bounds": [MIN_MEASURED_TRIALS, MAX_MEASURED_TRIALS],
        "default_seed": DEFAULT_SEED,
        "default_measurement_orders": orders,
        "fixed_configuration": EXPECTED_CONFIGURATION,
        "slo_thresholds": SLO_THRESHOLDS,
        "student_t_sample_sizes": [MIN_MEASURED_TRIALS, MAX_MEASURED_TRIALS],
        "child_environment_is_allowlisted": True,
        "provider_credential_names_not_inherited": list(PROVIDER_CREDENTIAL_ENV_KEYS),
        "child_process_isolation": True,
        "child_cleanup_grace_seconds": CHILD_CLEANUP_GRACE_SECONDS,
        "broad_docker_cleanup": False,
        "production_sla": False,
    }


def _validate_arguments(args: argparse.Namespace) -> None:
    require(
        MIN_MEASURED_TRIALS <= args.measured_trials <= MAX_MEASURED_TRIALS,
        f"--measured-trials must be between {MIN_MEASURED_TRIALS} and {MAX_MEASURED_TRIALS}",
    )
    require(not isinstance(args.seed, bool), "--seed must be an integer")
    require(
        args.seed == DEFAULT_SEED,
        f"formal profile seed is fixed at {DEFAULT_SEED}",
    )
    require(
        MIN_TRIAL_TIMEOUT_SECONDS <= args.trial_timeout_seconds <= MAX_TRIAL_TIMEOUT_SECONDS,
        (
            "--trial-timeout-seconds must be between "
            f"{MIN_TRIAL_TIMEOUT_SECONDS} and {MAX_TRIAL_TIMEOUT_SECONDS}"
        ),
    )


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--measured-trials",
        type=int,
        default=DEFAULT_MEASURED_TRIALS,
        help=f"measured trials after one warm-up ({MIN_MEASURED_TRIALS}..{MAX_MEASURED_TRIALS})",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--trial-timeout-seconds",
        type=int,
        default=DEFAULT_TRIAL_TIMEOUT_SECONDS,
        help="hard timeout for each isolated capacity child",
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=DEFAULT_ARTIFACTS_ROOT,
        help="gitignored repository-relative aggregate artifact root",
    )
    parser.add_argument(
        "--self-check-only",
        action="store_true",
        help="validate the pure contract without requiring clean Git or Docker",
    )
    args = parser.parse_args(argv)
    _validate_arguments(args)
    return args


def _git_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Remove worktree/index/config overrides before signing repository facts."""

    environment = dict(os.environ if source is None else source)
    for key in tuple(environment):
        if key in GIT_OVERRIDE_ENV_KEYS or key.startswith("GIT_CONFIG_"):
            environment.pop(key, None)
    return environment


def _child_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the minimum host environment needed by Git, Python, and local Docker."""

    inherited = os.environ if source is None else source
    environment = {
        key: value
        for key, value in inherited.items()
        if key in CHILD_ENV_ALLOWLIST or key.startswith("LC_")
    }
    return _git_environment(environment)


def _run_git(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        timeout=15,
        env=_git_environment(),
    )
    require(completed.returncode == 0, "Git repository preflight failed")
    return completed.stdout.strip()


def _repository_state(repository_root: Path) -> tuple[str, list[str]]:
    discovered_root = Path(_run_git(repository_root, "rev-parse", "--show-toplevel"))
    require(
        discovered_root.resolve(strict=True) == repository_root.resolve(strict=True),
        "Git repository root did not match the qualification source root",
    )
    commit = _run_git(repository_root, "rev-parse", "HEAD")
    status_lines = _run_git(
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).splitlines()
    return commit, status_lines


def _ensure_internal_artifact_root(repository_root: Path, value: Path) -> Path:
    root = repository_root.resolve(strict=True)
    lexical = value if value.is_absolute() else root / value
    lexical = Path(os.path.abspath(lexical))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise QualificationFailure("artifact root escaped the repository") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise QualificationFailure("artifact root contains a symlink")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(relative)],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
        env=_git_environment(),
    )
    require(ignored.returncode == 0, "artifact root is not covered by .gitignore")
    return lexical


def _atomic_write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        encoded = json.dumps(
            sanitize(evidence),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationFailure("aggregate evidence was not safe JSON") from exc
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _expected_hashes(repository_root: Path) -> dict[str, str]:
    paths = {
        "capacity_script_sha256": repository_root / "scripts/phase2_capacity.py",
        "acceptance_script_sha256": repository_root / "scripts/phase2_acceptance.py",
        "compose_sha256": repository_root / "compose.yaml",
        "slo_script_sha256": Path(__file__).resolve(),
    }
    for path in paths.values():
        require(path.is_file() and not path.is_symlink(), "qualification source file is missing")
    return {name: _sha256_file(path) for name, path in paths.items()}


def _child_command(
    repository_root: Path,
    artifacts_root: Path,
    measurement_order: str,
) -> list[str]:
    return [
        sys.executable,
        "-I",
        str(repository_root / "scripts/phase2_capacity.py"),
        "--workers",
        "2",
        "--runs-per-phase",
        "4",
        "--backlog-limit",
        "4",
        "--burst-runs",
        "6",
        "--submit-concurrency",
        "6",
        "--run-concurrency",
        "1",
        "--question-quantum",
        "5",
        "--mock-delay-seconds",
        "0.08",
        "--timeout-seconds",
        "180",
        "--lease-seconds",
        "30",
        "--heartbeat-seconds",
        "10",
        "--worker-poll-seconds",
        "1",
        "--worker-max-attempts",
        "3",
        "--retry-backoff-base-seconds",
        "1",
        "--retry-backoff-cap-seconds",
        "30",
        "--worker-shutdown-grace-seconds",
        "30",
        "--redis-block-milliseconds",
        "1000",
        "--redis-operation-timeout-seconds",
        "1",
        "--measurement-order",
        measurement_order,
        "--artifacts-root",
        str(artifacts_root.relative_to(repository_root)),
    ]


def _run_child(
    command: Sequence[str],
    *,
    repository_root: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    set_current_child: Callable[[subprocess.Popen[bytes] | None], None],
    termination_requested: Callable[[], bool],
) -> int:
    process = subprocess.Popen(
        list(command),
        cwd=repository_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    set_current_child(process)
    deadline = time.monotonic() + timeout_seconds
    termination_deadline: float | None = None
    try:
        while True:
            returncode = process.poll()
            if returncode is not None:
                return returncode
            now = time.monotonic()
            if termination_requested():
                if termination_deadline is None:
                    _signal_child_group(process, signal.SIGTERM)
                    termination_deadline = now + CHILD_CLEANUP_GRACE_SECONDS
                if now >= termination_deadline:
                    _signal_child_group(process, signal.SIGKILL)
                    process.wait(timeout=15)
                    raise QualificationInterrupted(
                        "capacity child exceeded termination cleanup grace"
                    )
            elif now >= deadline:
                _signal_child_group(process, signal.SIGTERM)
                try:
                    process.wait(timeout=CHILD_CLEANUP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    _signal_child_group(process, signal.SIGKILL)
                    process.wait(timeout=15)
                raise QualificationFailure("capacity child timed out after cleanup grace") from None
            time.sleep(0.25)
    except KeyboardInterrupt:
        if process.poll() is None:
            _signal_child_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=CHILD_CLEANUP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _signal_child_group(process, signal.SIGKILL)
                process.wait(timeout=15)
        raise
    finally:
        set_current_child(None)


def _signal_child_group(process: subprocess.Popen[bytes], received: signal.Signals) -> None:
    """Signal only the isolated capacity child's process group."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, received)
    except ProcessLookupError:
        return


def _single_child_evidence(
    trial_root: Path, repository_root: Path
) -> tuple[Path, dict[str, Any], str]:
    candidates = list(trial_root.rglob("evidence.json")) if trial_root.is_dir() else []
    require(len(candidates) == 1, "trial did not retain exactly one child evidence file")
    safe_path, raw = _read_repository_file(
        candidates[0], repository_root, max_bytes=MAX_CHILD_EVIDENCE_BYTES
    )
    try:
        parsed = _parse_strict_json(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise QualificationFailure("child evidence is not readable UTF-8") from exc
    return safe_path, parsed, hashlib.sha256(raw).hexdigest()


def _trial_reference(
    *,
    role: str,
    index: int,
    order: str,
    evidence_path: Path,
    evidence_sha256: str,
    repository_root: Path,
    validated: dict[str, Any],
) -> dict[str, Any]:
    return {
        "role": role,
        "index": index,
        "measurement_order": order,
        "evidence_path": evidence_path.relative_to(repository_root).as_posix(),
        "evidence_sha256": evidence_sha256,
        "status": "passed",
        "environment_fingerprint": validated["environment_fingerprint"],
        "configuration_fingerprint": validated["configuration_fingerprint"],
        "data_fingerprint": validated["data_fingerprint"],
        "metrics": validated["metrics"],
        "hard_invariants": validated["hard_invariants"],
    }


def run_suite(args: argparse.Namespace, repository_root: Path) -> tuple[dict[str, Any], Path]:
    """Run the fixed suite and return its final aggregate and evidence path."""

    repository_root = repository_root.resolve(strict=True)
    artifact_root = _ensure_internal_artifact_root(repository_root, args.artifacts_root)
    initial_commit, initial_status = _repository_state(repository_root)
    hashes = _expected_hashes(repository_root)
    suite_name = "llmbenchlab-p2-slo-{}-{}".format(
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), uuid.uuid4().hex[:12]
    )
    suite_directory = artifact_root / suite_name
    evidence_path = suite_directory / "evidence.json"
    orders = balanced_measurement_orders(args.seed, args.measured_trials)
    aggregate: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA,
        "status": "running",
        "started_at": utc_now(),
        "finished_at": None,
        "profile": {
            "name": PROFILE_NAME,
            "offline_only": True,
            "production_slo": False,
            "single_host_single_failure_domain": True,
            "support_boundary": (
                "Exact clean commit and fixed local PostgreSQL 16/Redis 7/Mock profile only; "
                "not a real Provider benchmark, HA proof, production SLA, or extrapolation."
            ),
        },
        "repository": {
            "commit": initial_commit,
            "dirty": bool(initial_status),
            **hashes,
        },
        "experiment": {
            "seed": args.seed,
            "warmup_trials": WARMUP_TRIALS,
            "measured_trials": args.measured_trials,
            "trial_timeout_seconds": args.trial_timeout_seconds,
            "warmup_measurement_order": "single_then_multi",
            "measured_measurement_orders": orders,
            "serial_execution": True,
            "discarded_trials": 0,
            "selection_boundary": (
                "All trials in this invocation are retained; local artifacts alone cannot prove "
                "that earlier suite invocations were not discarded."
            ),
        },
        "configuration": {
            "fixed": EXPECTED_CONFIGURATION,
            "slo_thresholds": SLO_THRESHOLDS,
        },
        "environment": {},
        "trials": [],
        "statistics": {},
        "slo_results": [],
        "capacity_model": {},
        "failure": None,
    }
    _atomic_write_evidence(evidence_path, aggregate)

    child_environment = _child_environment()

    current_child: subprocess.Popen[bytes] | None = None
    interrupted = False

    def set_current_child(process: subprocess.Popen[bytes] | None) -> None:
        nonlocal current_child
        current_child = process

    def forward_termination(_signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True

    previous_term = signal.signal(signal.SIGTERM, forward_termination)
    environment_fingerprint: str | None = None
    configuration_fingerprint: str | None = None
    data_fingerprint: str | None = None
    child_projects: set[str] = set()
    measured_validated: list[dict[str, Any]] = []
    try:
        require(not initial_status, "formal qualification requires a clean Git worktree")
        planned = [("warmup", 0, "single_then_multi")]
        planned.extend(("measured", index, order) for index, order in enumerate(orders, start=1))
        for role, index, order in planned:
            if interrupted:
                raise QualificationInterrupted("received termination signal")
            commit, status = _repository_state(repository_root)
            require(
                commit == initial_commit and not status, "repository changed during qualification"
            )
            trial_name = "warmup" if role == "warmup" else f"measured-{index:02d}"
            trial_root = suite_directory / "trials" / trial_name
            trial_root.mkdir(parents=True, exist_ok=False)
            command = _child_command(repository_root, trial_root, order)
            trial_record: dict[str, Any] = {
                "role": role,
                "index": index,
                "measurement_order": order,
                "status": "running",
            }
            aggregate["trials"].append(trial_record)
            _atomic_write_evidence(evidence_path, aggregate)
            try:
                returncode = _run_child(
                    command,
                    repository_root=repository_root,
                    environment=child_environment,
                    timeout_seconds=args.trial_timeout_seconds,
                    set_current_child=set_current_child,
                    termination_requested=lambda: interrupted,
                )
                if interrupted:
                    raise QualificationInterrupted(
                        "received termination signal after child cleanup"
                    )
                child_path, child_evidence, evidence_sha256 = _single_child_evidence(
                    trial_root, repository_root
                )
                trial_record.update(
                    evidence_path=child_path.relative_to(repository_root).as_posix(),
                    evidence_sha256=evidence_sha256,
                )
                project_name = str(child_evidence.get("project_name") or "")
                project_suffix = project_name.removeprefix("llmbenchlab-p2-")
                require(
                    project_name.startswith("llmbenchlab-p2-")
                    and len(project_suffix) == 12
                    and all(character in "0123456789abcdef" for character in project_suffix)
                    and project_name not in child_projects,
                    "capacity project identity was unsafe or reused",
                )
                require(
                    child_evidence.get("artifacts")
                    == child_path.relative_to(repository_root).as_posix(),
                    "child artifact path drift",
                )
                child_projects.add(project_name)
                require(returncode == 0, "capacity child exited unsuccessfully")
                validated = validate_child_evidence(
                    child_evidence,
                    expected_commit=initial_commit,
                    expected_hashes=hashes,
                    expected_order=order,
                    expected_environment_fingerprint=environment_fingerprint,
                    expected_configuration_fingerprint=configuration_fingerprint,
                    expected_data_fingerprint=data_fingerprint,
                )
            except BaseException as exc:
                trial_record["status"] = "failed"
                trial_record["failure_type"] = type(exc).__name__
                _atomic_write_evidence(evidence_path, aggregate)
                raise
            environment_fingerprint = validated["environment_fingerprint"]
            configuration_fingerprint = validated["configuration_fingerprint"]
            data_fingerprint = validated["data_fingerprint"]
            aggregate["environment"] = {
                "fingerprint": environment_fingerprint,
                "normalized": validated["environment"],
            }
            aggregate["configuration"]["normalized_fingerprint"] = configuration_fingerprint
            aggregate["configuration"]["data_fingerprint"] = data_fingerprint
            trial_record.clear()
            trial_record.update(
                _trial_reference(
                    role=role,
                    index=index,
                    order=order,
                    evidence_path=child_path,
                    evidence_sha256=evidence_sha256,
                    repository_root=repository_root,
                    validated=validated,
                )
            )
            if role == "measured":
                measured_validated.append(validated)
            _atomic_write_evidence(evidence_path, aggregate)

        if interrupted:
            raise QualificationInterrupted("received termination signal before final evaluation")
        final_commit, final_status = _repository_state(repository_root)
        require(
            final_commit == initial_commit and not final_status,
            "repository changed before final SLO evaluation",
        )
        require(_expected_hashes(repository_root) == hashes, "qualification source hashes changed")
        evaluation = evaluate_suite(measured_validated)
        aggregate["statistics"] = evaluation["statistics"]
        aggregate["slo_results"] = evaluation["slo_results"]
        aggregate["capacity_model"] = evaluation["capacity_model"]
        aggregate["status"] = evaluation["status"]
        if aggregate["status"] != "passed":
            aggregate["failure"] = {
                "type": "SLOObjectiveFailure",
                "message": "one or more preregistered SLO objectives failed",
            }
    except BaseException as exc:
        aggregate["status"] = "failed"
        aggregate["failure"] = {
            "type": type(exc).__name__,
            "message": redact_text(str(exc))[:1000],
        }
        with contextlib.suppress(AttributeError):
            exc.evidence_path = evidence_path
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        aggregate["finished_at"] = utc_now()
        _atomic_write_evidence(evidence_path, aggregate)
    return aggregate, evidence_path


def main(argv: Sequence[str]) -> int:
    try:
        args = parse_arguments(argv)
        if args.self_check_only:
            print(
                json.dumps(
                    self_check_contract(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
            )
            return 0
        aggregate, evidence_path = run_suite(args, Path(__file__).resolve().parents[1])
    except (QualificationFailure, KeyboardInterrupt) as exc:
        print(f"Phase 2 SLO qualification failed: {redact_text(str(exc))}", file=sys.stderr)
        retained = getattr(exc, "evidence_path", None)
        if retained is not None:
            print(f"Evidence retained at: {retained}", file=sys.stderr)
        return 1
    except BaseException as exc:
        print(
            f"Unexpected Phase 2 SLO qualification failure: {type(exc).__name__}: "
            f"{redact_text(str(exc))}",
            file=sys.stderr,
        )
        return 1
    print(f"Phase 2 SLO status: {aggregate['status']}")
    print(f"Evidence retained at: {evidence_path}")
    return 0 if aggregate["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
