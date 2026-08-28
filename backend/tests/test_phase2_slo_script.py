"""Strictly offline contract tests for the Phase 2 SLO qualification harness."""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import signal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SLO_SCRIPT = _REPOSITORY_ROOT / "scripts" / "phase2_slo.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase2_slo_script", _SLO_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, allow_nan=False, ensure_ascii=False),
        encoding="utf-8",
    )


def _zero_gauges() -> dict[str, int]:
    return {field: 0 for field in script.ZERO_GAUGE_FIELDS}


def _container_limit(service: str, identity: str) -> dict[str, object]:
    return {
        "service": service,
        "container_id": f"container-{identity}",
        "hostname": f"hostname-{identity}",
        "image_id": f"sha256:{'a' * 64}",
        "image_content_sha256": "b" * 64,
        "pid": 100,
        "memory_limit_bytes": 0,
        "memory_swap_limit_bytes": 0,
        "nano_cpus": 0,
        "cpu_quota": 0,
        "cpu_period": 0,
        "pids_limit": None,
    }


def _environment(identity: str = "a") -> dict[str, Any]:
    return {
        "host": {
            "operating_system": "Darwin",
            "os_release": "fixture",
            "architecture": "arm64",
            "cpu_model": "fixture-cpu",
            "logical_cpu_count": 8,
            "memory_bytes": 8_589_934_592,
            "python_version": "3.11.15",
        },
        "docker": {
            "server_version": "29.7.2",
            "operating_system": "Docker Desktop",
            "architecture": "aarch64",
            "logical_cpu_count": 8,
            "memory_bytes": 4_108_632_064,
            "rootless": False,
        },
        "postgres_version": "16.14",
        "postgres_max_connections": 100,
        "redis_version": "7.4.10",
        "runtime_settings": {
            "database_pool_size": script.EXPECTED_CONFIGURATION["database_pool_size"],
            "database_max_overflow": script.EXPECTED_CONFIGURATION["database_max_overflow"],
            "database_pool_timeout_seconds": script.EXPECTED_CONFIGURATION[
                "database_pool_timeout_seconds"
            ],
            "readiness_database_timeout_seconds": script.EXPECTED_CONFIGURATION[
                "readiness_database_timeout_seconds"
            ],
            "worker_lease_seconds": script.EXPECTED_CONFIGURATION["lease_seconds"],
            "worker_heartbeat_seconds": script.EXPECTED_CONFIGURATION["heartbeat_seconds"],
            "worker_poll_seconds": script.EXPECTED_CONFIGURATION["worker_poll_seconds"],
            "worker_max_attempts": script.EXPECTED_CONFIGURATION["worker_max_attempts"],
            "worker_retry_backoff_base_seconds": script.EXPECTED_CONFIGURATION[
                "retry_backoff_base_seconds"
            ],
            "worker_retry_backoff_cap_seconds": script.EXPECTED_CONFIGURATION[
                "retry_backoff_cap_seconds"
            ],
            "worker_shutdown_grace_seconds": script.EXPECTED_CONFIGURATION[
                "worker_shutdown_grace_seconds"
            ],
            "redis_block_milliseconds": script.EXPECTED_CONFIGURATION["redis_block_milliseconds"],
            "redis_operation_timeout_seconds": script.EXPECTED_CONFIGURATION[
                "redis_operation_timeout_seconds"
            ],
        },
        "container_limits": {
            "postgres": [_container_limit("postgres", f"postgres-{identity}")],
            "redis": [_container_limit("redis", f"redis-{identity}")],
            "api": [_container_limit("api", f"api-{identity}")],
            "workers": [
                _container_limit("worker", f"worker-1-{identity}"),
                _container_limit("worker", f"worker-2-{identity}"),
            ],
        },
    }


def _latency_distribution(value: float, *, count: int = 4) -> dict[str, object]:
    return {
        "count": count,
        "min": value,
        "mean": value,
        "p50": value,
        "p95": value,
        "p99": value,
        "max": value,
        "samples": [value] * count,
    }


def _measurement_run_ids(name: str) -> list[str]:
    offset = {
        "single_worker_reference": 100,
        "configured_multi_worker_baseline": 200,
        "warmed_pause_burst_and_drain": 300,
        "cold_start_burst_and_drain": 400,
    }[name]
    return [f"00000000-0000-4000-8000-{offset + index:012x}" for index in range(1, 5)]


def _measurement(name: str) -> dict[str, Any]:
    burst = name in script.BURST_MEASUREMENT_NAMES
    run_ids = _measurement_run_ids(name)
    status_counts = {"202": 4, "429": 2} if burst else {"202": 4}
    questions_per_second = {
        "single_worker_reference": 7.0,
        "configured_multi_worker_baseline": 13.0,
        "warmed_pause_burst_and_drain": 8.0,
        "cold_start_burst_and_drain": 8.0,
    }[name]
    measurement: dict[str, Any] = {
        "name": name,
        "workers": 1 if name == "single_worker_reference" else 2,
        "submission": {
            "requested": 6 if burst else 4,
            "accepted": 4,
            "rejected": 2 if burst else 0,
            "status_counts": status_counts,
        },
        "throughput": {
            "questions_per_second": questions_per_second,
            "completed_runs": 4,
            "completed_questions": 60,
        },
        "wall_duration_seconds": 60 / questions_per_second,
        "response_count": 60,
        "errors_and_retries": {
            "terminal_statuses": {"completed": 4},
            "question_errors": 0,
            "failed_attempt_count": 0,
        },
        "provider_attempts": {
            "provider_reservations": 60,
            "settled_actual_reservations": 60,
            "settled_conservative_reservations": 0,
            "attempts_per_completed_question": 1.0,
        },
        "cooperative_scheduling": {
            "all_runs_dispatched_more_than_once": True,
            "all_runs_yielded": True,
            "per_run": [
                {
                    "run_id": run_id,
                    "dispatch_count": 3,
                    "claim_events": 3,
                    "cooperative_yield_events": 2,
                }
                for run_id in run_ids
            ],
        },
        "latency_seconds": {
            "queue": _latency_distribution(1.0),
            "execution": _latency_distribution(2.0),
            "end_to_end": _latency_distribution(3.0),
        },
        "question_latency_ms": _latency_distribution(1.0, count=60),
        "database": {"task_metrics": {"final_database_gauges": _zero_gauges()}},
        "queue": {"after": {"group": {"pending": 0, "lag": 0}}},
        "run_ids": run_ids,
    }
    if burst:
        workers = [
            {
                "container_id": str(index) * 64,
                "hostname": f"{name}-worker-{index}",
            }
            for index in range(1, 3)
        ]
        claim_times = [
            "2026-08-28T00:00:02.100000Z",
            "2026-08-28T00:00:02.200000Z",
            "2026-08-28T00:00:02.300000Z",
            "2026-08-28T00:00:02.400000Z",
        ]
        claims = [
            {
                "run_id": run_id,
                "worker_id": (
                    f"worker:{workers[index % 2]['hostname']}:{index % 2 + 1}:"
                    f"00000000-0000-4000-8000-00000000000{index % 2 + 1}"
                ),
                "occurred_at": claim_times[index],
            }
            for index, run_id in enumerate(run_ids)
        ]
        measurement.update(
            mode="exact_backlog_limit_concurrent_202_429_then_drain",
            barrier=("warmed_pause" if name == "warmed_pause_burst_and_drain" else "cold_start"),
            configured_backlog_limit=4,
            expected_status_counts=status_counts,
            observed_status_counts=status_counts,
            typed_rejections=[
                {
                    "status_code": 429,
                    "payload": {"detail": {"code": "run_backlog_full", "limit": 4}},
                }
                for _ in range(2)
            ],
            accepted_runs_preserved=True,
            accepted_run_ids=list(run_ids),
            backlog_after_drain=_zero_gauges(),
            backlog_drain_seconds=6.0,
            worker_participation={
                "validated_workers": workers,
                "claims": claims,
                "distinct_claim_workers": 2,
                "all_claim_workers_validated": True,
            },
            worker_state_after_restart=[
                {
                    "id": worker["container_id"],
                    "hostname": worker["hostname"],
                    "project": "llmbenchlab-p2-aaaaaaaaaaaa",
                    "service": "worker",
                    "status": "running",
                    "health": "healthy",
                }
                for worker in workers
            ],
            timing={
                "clock_domains": {
                    "monotonic_seconds": "process_monotonic",
                    "durable_utc": "database_utc",
                },
                "monotonic_seconds": {
                    "suspend": 0.2,
                    "backlog_build": 1.5,
                    "restore_command": 0.25,
                    "drain": 6.0,
                },
                "durable_utc": {
                    "suspend_completed_at": "2026-08-28T00:00:00Z",
                    "backlog_ready_at": "2026-08-28T00:00:01Z",
                    "restore_completed_at": "2026-08-28T00:00:02Z",
                    "first_claim_at": claim_times[0],
                    "all_workers_first_claim_at": claim_times[1],
                    "claim_or_yield_events": [
                        *[
                            {
                                "run_id": run_id,
                                "event_type": "run_claimed",
                                "occurred_at": claim_times[index],
                            }
                            for index, run_id in enumerate(run_ids)
                        ],
                        *[
                            {
                                "run_id": run_id,
                                "event_type": "run_yielded",
                                "occurred_at": f"2026-08-28T00:00:02.{index + 5}00000Z",
                            }
                            for index, run_id in enumerate(run_ids)
                        ],
                    ],
                    "run_first_claim_to_finish": [
                        {
                            "run_id": run_id,
                            "first_claim_at": claim_times[index],
                            "finished_at": f"2026-08-28T00:00:06.{index + 1}00000Z",
                            "duration_seconds": 4.0,
                        }
                        for index, run_id in enumerate(run_ids)
                    ],
                },
                "durable_seconds": {
                    "backlog_ready_to_first_claim": 1.1,
                    "backlog_ready_to_all_workers_first_claim": 1.2,
                    "adjacent_claim_or_yield_gap": _latency_distribution(0.1, count=7),
                    "first_claim_to_finish": _latency_distribution(4.0),
                },
            },
        )
    return measurement


FAIRNESS_HIGH_MODEL_ID = "10000000-0000-4000-8000-000000000001"
FAIRNESS_LOW_MODEL_ID = "10000000-0000-4000-8000-000000000002"


def _fairness_evidence() -> dict[str, Any]:
    high_model_id = FAIRNESS_HIGH_MODEL_ID
    low_model_id = FAIRNESS_LOW_MODEL_ID
    high_run_ids = [f"20000000-0000-4000-8000-{index:012x}" for index in range(1, 4)]
    low_run_id = "20000000-0000-4000-8000-000000000004"
    all_run_ids = [*high_run_ids, low_run_id]
    event_specs: list[tuple[str, str, str]] = []
    for round_number in range(3):
        for index, run_id in enumerate(all_run_ids, start=1):
            event_specs.append(
                (
                    run_id,
                    "run_claimed",
                    f"2026-08-28T00:00:0{round_number * 2 + 1}.{index}00000Z",
                )
            )
        event_type = "run_yielded" if round_number < 2 else "run_terminal"
        for index, run_id in enumerate(all_run_ids, start=5):
            event_specs.append(
                (
                    run_id,
                    event_type,
                    f"2026-08-28T00:00:0{round_number * 2 + 1}.{index}00000Z",
                )
            )
    ordered_events = [
        {
            "event_id": f"30000000-0000-4000-8000-{index:012x}",
            "event_type": event_type,
            "occurred_at": occurred_at,
            "run_id": run_id,
            "role": "low_volume" if run_id == low_run_id else "high_volume",
        }
        for index, (run_id, event_type, occurred_at) in enumerate(event_specs, start=1)
    ]
    observation_fields = {
        "status": "pending",
        "completed_questions": 5,
        "dispatch_count": 1,
        "last_scheduled_at": "2026-08-28T00:00:01Z",
    }
    return {
        "name": "cross_model_fair_quantum_ordering",
        "question_quantum": 5,
        "configured_backlog_limit": 4,
        "high_volume_model_id": high_model_id,
        "low_volume_model_id": low_model_id,
        "high_volume_run_ids": high_run_ids,
        "low_volume_run_id": low_run_id,
        "ordering_evidence": {
            "low_volume_claim_observed": True,
            "low_volume_slice_observed": True,
            "low_claim_before_high_backlog_drained": True,
            "high_volume_incomplete_at_low_slice": 3,
            "observation": {
                "low_run": {
                    "id": low_run_id,
                    "model_id": low_model_id,
                    **observation_fields,
                    "status": "running",
                },
                "high_runs": [
                    {"id": run_id, "model_id": high_model_id, **observation_fields}
                    for run_id in high_run_ids
                ],
            },
            "ordered_events": ordered_events,
        },
        "terminal_runs": [
            {
                "id": run_id,
                "model_id": low_model_id if run_id == low_run_id else high_model_id,
                "status": "completed",
                "completed_questions": 15,
                "dispatch_count": 3,
            }
            for run_id in (*high_run_ids, low_run_id)
        ],
        "backlog_after_drain": _zero_gauges(),
    }


def _expected_hashes() -> dict[str, str]:
    return {
        "capacity_script_sha256": "a" * 64,
        "acceptance_script_sha256": "b" * 64,
        "compose_sha256": "c" * 64,
    }


def _happy_child(
    order: str = "single_then_multi",
    *,
    commit: str = "d" * 40,
    identity: str = "a",
) -> dict[str, Any]:
    topology_entry = {"status": "running", "health": "healthy"}
    reconciliation_database = {field: 0 for field in script.ZERO_RECONCILIATION_FIELDS}
    reconciliation_database.update(
        policies=2,
        active_policies=1,
        runs=22,
        responses=330,
        distinct_run_question_responses=330,
        question_executions=330,
        reservations=331,
        failed_attempt_count=1,
        reservation_states={"settled_actual": 330, "settled_conservative": 1},
        audit_events=993,
        audit_event_types={
            "provider_attempt_reserved": 331,
            "provider_attempt_send_started": 331,
            "provider_attempt_settled": 331,
        },
    )
    hashes = _expected_hashes()
    return {
        "schema_version": script.CAPACITY_EVIDENCE_SCHEMA,
        "project_name": "llmbenchlab-p2-aaaaaaaaaaaa",
        "status": "passed",
        "offline_only": True,
        "production_slo": False,
        "failure": None,
        "repository": {
            "commit": commit,
            "dirty": False,
            "status_paths": [],
            **hashes,
        },
        "configuration": {
            **script.EXPECTED_CONFIGURATION,
            "measurement_order": order,
        },
        "environment": _environment(identity),
        "data": {
            "demo_only": True,
            "protocol_version": "llmbenchlab-protocol-v1",
            "benchmark": {
                **script.EXPECTED_DEMO,
            },
            "model": {
                "id": FAIRNESS_HIGH_MODEL_ID,
                "provider_type": "mock",
                "enabled": True,
                "api_key_env": None,
                "base_url": None,
            },
            "fairness_low_volume_model": {
                "id": FAIRNESS_LOW_MODEL_ID,
                "provider_type": "mock",
                "enabled": True,
                "api_key_env": None,
                "base_url": None,
            },
            "governance_policy": {
                "is_active": True,
                "limits": script.EXPECTED_POLICY_LIMITS,
            },
        },
        "topology": {
            "postgres": [topology_entry],
            "redis": [topology_entry],
            "api": [topology_entry],
            "workers": [topology_entry, topology_entry],
            "ready": {"status": "ready", "database": "ok", "queue": "ok", "schema": "ok"},
        },
        "self_review": {
            "status": "passed",
            "mock_only": True,
            "finite_governance_policy": True,
            "real_provider_credentials_removed": list(script.PROVIDER_CREDENTIAL_ENV_KEYS),
        },
        "measurements": [_measurement(name) for name in script.MEASUREMENT_ORDERS[order]],
        "fairness": _fairness_evidence(),
        "faults": [
            {
                "name": "lease_owner_sigkill_and_expiry_recovery",
                "terminal": {
                    "status": "completed",
                    "completed_questions": 15,
                    "total_questions": 15,
                    "error_questions": 0,
                    "attempt_count": 2,
                    "failed_attempt_count": 1,
                },
                "victim_after_kill": {"status": "exited", "exit_code": 137},
                "task_metrics": {"final_database_gauges": _zero_gauges()},
                "timing": {
                    "kill_fence_database_at": "2026-08-28T00:00:00Z",
                    "old_lease_expires_at": "2026-08-28T00:00:32Z",
                    "reclaim_occurred_at": "2026-08-28T00:00:37Z",
                    "kill_fence_to_reclaim_seconds": 37.0,
                    "lease_expiry_to_reclaim_seconds": 5.0,
                },
            },
            {
                "name": "redis_stop_start_database_reconciliation",
                "pending_last_error": "queue_notification_unavailable",
                "terminal_status": "completed",
                "ready_while_stopped": {
                    "status": "degraded",
                    "database": "ok",
                    "queue": "unavailable",
                    "accepting_runs": True,
                },
                "workers_while_redis_stopped": [
                    {"status": "running", "health": "healthy"},
                    {"status": "running", "health": "healthy"},
                ],
                "task_metrics": {"final_database_gauges": _zero_gauges()},
                "timing": {
                    "run_created_at": "2026-08-28T00:00:00Z",
                    "first_claim_occurred_at": "2026-08-28T00:00:01Z",
                    "terminal_at": "2026-08-28T00:00:02Z",
                    "run_created_to_claim_seconds": 1.0,
                    "run_created_to_terminal_seconds": 2.0,
                },
            },
            {
                "name": "duplicate_terminal_delivery_noop",
                "snapshot_sha256": "f" * 64,
                "before_snapshot_sha256": "f" * 64,
                "after_snapshot_sha256": "f" * 64,
                "queue_after_ack": {"pending": 0, "lag": 0},
            },
        ],
        "reconciliation": {
            "database": reconciliation_database,
            "queue": {"group": {"pending": 0, "lag": 0}},
            "task_metrics": _zero_gauges(),
            "workers": [
                {"status": "running", "health": "healthy"},
                {"status": "running", "health": "healthy"},
            ],
        },
        "cleanup": {
            "status": "passed",
            "down_returncode": 0,
            "remaining_containers": [],
            "remaining_project_volumes": [],
            "remaining_project_networks": [],
            "image_cleanup_status": "passed",
            "project_image_candidates": 1,
            "removed_project_images": 1,
            "retained_shared_project_images": 0,
            "remaining_project_images": 0,
        },
    }


def _burst_measurement(child: dict[str, Any], name: str) -> dict[str, Any]:
    return next(measurement for measurement in child["measurements"] if measurement["name"] == name)


def test_sample_statistics_use_student_t_and_report_variation() -> None:
    statistics = script.sample_statistics([1.0, 2.0, 3.0, 4.0, 5.0])

    assert statistics["n"] == 5
    assert statistics["mean"] == 3.0
    assert statistics["median"] == 3.0
    assert statistics["min"] == 1.0
    assert statistics["max"] == 5.0
    assert statistics["sample_std"] == pytest.approx(math.sqrt(2.5))
    assert statistics["cv"] == pytest.approx(math.sqrt(2.5) / 3.0)
    assert statistics["two_sided_95_ci"] == pytest.approx([1.0367568385, 4.9632431615])
    assert statistics["one_sided_95_lcb"] == pytest.approx(1.4925566809)
    assert statistics["one_sided_95_ucb"] == pytest.approx(4.5074433191)
    assert statistics["p99_descriptive"] == pytest.approx(4.96)


def test_sample_statistics_handle_zero_variance_without_division_by_zero() -> None:
    statistics = script.sample_statistics([7.5] * 5)

    assert statistics["sample_std"] == 0.0
    assert statistics["cv"] == 0.0
    assert statistics["two_sided_95_ci"] == [7.5, 7.5]
    assert statistics["one_sided_95_lcb"] == 7.5
    assert statistics["one_sided_95_ucb"] == 7.5


@pytest.mark.parametrize(
    "values, match",
    [
        ([1.0, 2.0], "at least 3"),
        ([True, 1.0, 2.0], "numeric"),
        ([1.0, float("nan"), 2.0], "finite"),
        ([1.0, float("inf"), 2.0], "finite"),
    ],
)
def test_finite_samples_reject_invalid_statistical_inputs(values: list[float], match: str) -> None:
    with pytest.raises(script.QualificationFailure, match=match):
        script.finite_samples(values, name="throughput", minimum=3)


def test_zero_event_upper_bound_is_exact_and_rejects_invalid_counts() -> None:
    assert script.zero_event_upper_bound(300) == pytest.approx(1.0 - math.pow(0.05, 1.0 / 300.0))
    assert script.zero_event_upper_bound(1, confidence=0.95) == 0.95

    for total in (0, -1, True):
        with pytest.raises(script.QualificationFailure):
            script.zero_event_upper_bound(total)
    with pytest.raises(script.QualificationFailure, match="confidence"):
        script.zero_event_upper_bound(300, confidence=1.0)


def test_balanced_measurement_order_is_seeded_stable_and_two_sided() -> None:
    first = script.balanced_measurement_orders(seed=20260828, measured_trials=5)
    again = script.balanced_measurement_orders(seed=20260828, measured_trials=5)

    assert first == again
    assert len(first) == 5
    assert set(first) == {"single_then_multi", "multi_then_single"}
    assert abs(first.count("single_then_multi") - first.count("multi_then_single")) == 1


def test_slo_cli_defaults_bounds_and_self_check_mode() -> None:
    args = script.parse_arguments([])

    assert script.WARMUP_TRIALS == 1
    assert args.measured_trials == 5
    assert args.seed == 20260828
    assert args.trial_timeout_seconds == 900
    assert args.artifacts_root == Path(".pytest_cache/artifacts/phase2-slo")
    assert args.self_check_only is False
    assert script.parse_arguments(["--self-check-only"]).self_check_only is True

    for count in (4, 6):
        with pytest.raises(script.QualificationFailure, match="exactly 5"):
            script.parse_arguments(["--measured-trials", str(count)])
    for timeout in (299, 3601):
        with pytest.raises(script.QualificationFailure, match="between 300 and 3600"):
            script.parse_arguments(["--trial-timeout-seconds", str(timeout)])


def test_self_check_freezes_mock_profile_and_make_entrypoint() -> None:
    review = script.self_check_contract()
    makefile = (_REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")

    assert review["status"] == "passed"
    assert review["profile"] == script.PROFILE_NAME
    assert review["warmup_trials"] == 1
    assert review["default_measured_trials"] == 5
    assert review["fixed_measured_trials"] == 5
    assert review["student_t_sample_sizes"] == [5]
    assert review["fixed_configuration"]["mock_generation_delay_seconds"] == 0.08
    assert review["production_sla"] is False
    assert "phase2-slo:" in makefile
    assert "python3 -I scripts/phase2_slo.py" in makefile


@pytest.mark.parametrize("count", [4, 6])
def test_formal_profile_rejects_non_five_order_and_evaluation_samples(count: int) -> None:
    with pytest.raises(script.QualificationFailure, match="exactly 5"):
        script.balanced_measurement_orders(seed=20260828, measured_trials=count)
    with pytest.raises(script.QualificationFailure, match="exactly 5"):
        script.evaluate_suite([_evaluated_trial() for _ in range(count)])


def test_v2_contract_freezes_schema_profile_four_cells_and_dual_thresholds() -> None:
    assert script.EVIDENCE_SCHEMA == "llmbenchlab-phase2-slo-evidence-v2"
    assert script.CAPACITY_EVIDENCE_SCHEMA == "llmbenchlab-phase2-capacity-evidence-v2"
    assert script.PROFILE_NAME == "P2-local-control-plane-v2"
    assert script.EXPECTED_CONFIGURATION["qualification_profile"] == script.PROFILE_NAME
    assert {
        field: script.EXPECTED_CONFIGURATION[field]
        for field in (
            "timeout_seconds",
            "lease_seconds",
            "heartbeat_seconds",
            "worker_poll_seconds",
        )
    } == {
        "timeout_seconds": 180.0,
        "lease_seconds": 30.0,
        "heartbeat_seconds": 10.0,
        "worker_poll_seconds": 1.0,
    }
    assert script.MEASUREMENT_ORDERS == {
        "single_then_multi": (
            "single_worker_reference",
            "configured_multi_worker_baseline",
            "warmed_pause_burst_and_drain",
            "cold_start_burst_and_drain",
        ),
        "multi_then_single": (
            "configured_multi_worker_baseline",
            "single_worker_reference",
            "warmed_pause_burst_and_drain",
            "cold_start_burst_and_drain",
        ),
    }
    assert script.SLO_THRESHOLDS["latency_p95_seconds"]["warmed_pause_burst_and_drain"] == {
        "queue": 3.0,
        "execution": 5.0,
        "end_to_end": 8.0,
    }
    assert script.SLO_THRESHOLDS["latency_p95_seconds"]["cold_start_burst_and_drain"] == {
        "queue": 6.0,
        "execution": 8.0,
        "end_to_end": 10.0,
    }
    for name in script.BURST_MEASUREMENT_NAMES:
        assert script.SLO_THRESHOLDS["throughput"][name] == {
            "lcb_questions_per_second": 6.0,
            "max_cv": 0.20,
        }
        assert script.SLO_THRESHOLDS["backlog_drain_seconds"][name] == 10.0


def test_strict_json_loader_accepts_only_finite_unique_bounded_regular_files(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.json"
    _write_json(valid, {"safe": [1, 2.5, "Mock-only"]})
    assert script.strict_json_load(valid, tmp_path) == {"safe": [1, 2.5, "Mock-only"]}

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"status":"passed","status":"failed"}', encoding="utf-8")
    with pytest.raises(script.QualificationFailure, match="duplicate"):
        script.strict_json_load(duplicate, tmp_path)

    non_finite = tmp_path / "non-finite.json"
    non_finite.write_text('{"throughput":NaN}', encoding="utf-8")
    with pytest.raises(script.QualificationFailure, match="NaN"):
        script.strict_json_load(non_finite, tmp_path)

    oversized = tmp_path / "oversized.json"
    _write_json(oversized, {"value": "x" * 128})
    with pytest.raises(script.QualificationFailure, match="size"):
        script.strict_json_load(oversized, tmp_path, max_bytes=32)


def test_strict_json_loader_rejects_path_escape_and_symlink(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    outside = tmp_path / "outside.json"
    _write_json(outside, {"status": "passed"})

    with pytest.raises(script.QualificationFailure, match="repository"):
        script.strict_json_load(outside, evidence_root)

    symlink = evidence_root / "evidence.json"
    symlink.symlink_to(outside)
    with pytest.raises(script.QualificationFailure, match="symlink"):
        script.strict_json_load(symlink, evidence_root)


def test_normalized_environment_fingerprint_ignores_only_runtime_container_identity() -> None:
    base = _environment("a")
    changed_identity = _environment("b")
    changed_identity["container_limits"]["api"][0]["image_id"] = f"sha256:{'c' * 64}"
    changed_environment = _environment("a")
    changed_environment["host"]["os_release"] = "changed"
    changed_image_content = _environment("a")
    changed_image_content["container_limits"]["api"][0]["image_content_sha256"] = "d" * 64

    assert script.normalized_fingerprint(
        script._normalize_environment(base)
    ) == script.normalized_fingerprint(script._normalize_environment(changed_identity))

    malformed_raw_identity = _environment("a")
    malformed_raw_identity["container_limits"]["api"][0]["image_id"] = f"sha256:{'C' * 64}"
    with pytest.raises(script.QualificationFailure, match="image identity is missing"):
        script._normalize_environment(malformed_raw_identity)
    assert script.normalized_fingerprint(
        script._normalize_environment(base)
    ) != script.normalized_fingerprint(script._normalize_environment(changed_environment))
    assert script.normalized_fingerprint(
        script._normalize_environment(base)
    ) != script.normalized_fingerprint(script._normalize_environment(changed_image_content))
    with pytest.raises(script.QualificationFailure, match="fingerprint"):
        script.normalized_fingerprint({"not_finite": float("nan")})


def test_child_validator_happy_path_accepts_balanced_order_and_dynamic_container_ids() -> None:
    expected_commit = "d" * 40
    first = script.validate_child_evidence(
        _happy_child("single_then_multi", identity="first"),
        expected_commit=expected_commit,
        expected_hashes=_expected_hashes(),
        expected_order="single_then_multi",
    )
    second = script.validate_child_evidence(
        _happy_child("multi_then_single", identity="second"),
        expected_commit=expected_commit,
        expected_hashes=_expected_hashes(),
        expected_order="multi_then_single",
        expected_environment_fingerprint=first["environment_fingerprint"],
        expected_configuration_fingerprint=first["configuration_fingerprint"],
        expected_data_fingerprint=first["data_fingerprint"],
    )

    assert set(first) == {
        "environment",
        "environment_fingerprint",
        "configuration_fingerprint",
        "data_fingerprint",
        "metrics",
        "hard_invariants",
    }
    assert first["environment_fingerprint"] == second["environment_fingerprint"]
    assert first["configuration_fingerprint"] == second["configuration_fingerprint"]
    assert first["data_fingerprint"] == second["data_fingerprint"]
    assert set(first["metrics"]["measurements"]) == set(script.MEASUREMENT_NAMES)
    assert all(first["hard_invariants"].values())
    assert first["metrics"]["fairness"] == {
        "low_volume_claim_observed": True,
        "low_volume_slice_observed": True,
        "low_claim_before_high_backlog_drained": True,
    }
    for name in script.BURST_MEASUREMENT_NAMES:
        burst = first["metrics"]["measurements"][name]
        assert burst["worker_participation"] == {
            "validated_worker_count": 2,
            "distinct_claim_worker_count": 2,
            "all_claim_workers_validated": True,
            "accepted_runs_with_claims": 4,
        }
        assert burst["timing_seconds"]["monotonic"] == {
            "suspend": 0.2,
            "backlog_build": 1.5,
            "restore_command": 0.25,
            "drain": 6.0,
        }


@pytest.mark.parametrize("name", script.BURST_MEASUREMENT_NAMES)
@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("status_split", "submission status"),
        ("barrier", "barrier"),
        ("typed_rejection", "typed backlog rejection count"),
        ("boolean_worker_count", "not an integer"),
        ("malformed_container", "container ID"),
        ("runtime_worker", "healthy project container"),
        ("runtime_worker_extra", "runtime Worker field set drift"),
        ("unmapped_worker", "did not map"),
        ("wrong_uuid_version", "did not map"),
        ("pid_out_of_range", "did not map"),
        ("one_claim_worker", "two distinct"),
        ("claim_before_backlog", "duration is negative"),
        ("throughput_wall", "submission through terminal"),
        ("drain_boundary", "restore invocation"),
        ("accepted_identity_partition", "did not match measurement Run IDs"),
        ("accepted_run_duplicate", "accepted Run IDs were not unique"),
        ("measurement_run_duplicate", "measurement Run IDs were not unique"),
        ("scheduling_run_duplicate", "scheduling Run IDs were not unique"),
        ("scheduling_run_identity", "did not match measurement Run IDs"),
        ("malformed_run_identity", "canonical UUIDv4"),
        ("foreign_timing_run", "escaped the burst boundary"),
        ("untyped_participation_claim", "typed timing events"),
        ("run_duration", "per-Run duration drift"),
        ("durable_duration", "duration drift"),
    ],
)
def test_burst_validator_fails_closed_on_admission_worker_and_segmented_timing_drift(
    name: str,
    case: str,
    match: str,
) -> None:
    child = _happy_child()
    burst = _burst_measurement(child, name)
    if case == "status_split":
        burst["submission"]["status_counts"] = {"202": 5, "429": 1}
    elif case == "barrier":
        burst["barrier"] = "cold_start" if name.startswith("warmed") else "warmed_pause"
    elif case == "typed_rejection":
        burst["typed_rejections"].pop()
    elif case == "boolean_worker_count":
        burst["worker_participation"]["distinct_claim_workers"] = True
    elif case == "malformed_container":
        burst["worker_participation"]["validated_workers"][0]["container_id"] = "A" * 64
    elif case == "runtime_worker":
        burst["worker_state_after_restart"][0]["health"] = "unhealthy"
    elif case == "runtime_worker_extra":
        burst["worker_state_after_restart"][0]["pid"] = 123
    elif case == "unmapped_worker":
        burst["worker_participation"]["claims"][0]["worker_id"] = (
            "worker:not-a-validated-host:1:00000000-0000-4000-8000-000000000001"
        )
    elif case == "wrong_uuid_version":
        burst["worker_participation"]["claims"][0]["worker_id"] = (
            f"worker:{burst['worker_participation']['validated_workers'][0]['hostname']}:1:"
            "00000000-0000-1000-8000-000000000001"
        )
    elif case == "pid_out_of_range":
        burst["worker_participation"]["claims"][0]["worker_id"] = (
            f"worker:{burst['worker_participation']['validated_workers'][0]['hostname']}:"
            "2147483648:00000000-0000-4000-8000-000000000001"
        )
    elif case == "one_claim_worker":
        first_owner = burst["worker_participation"]["claims"][0]["worker_id"]
        for claim in burst["worker_participation"]["claims"]:
            claim["worker_id"] = first_owner
    elif case == "claim_before_backlog":
        burst["worker_participation"]["claims"][0]["occurred_at"] = "2026-08-28T00:00:00.500000Z"
    elif case == "throughput_wall":
        burst["timing"]["monotonic_seconds"]["backlog_build"] = 1.6
    elif case == "drain_boundary":
        burst["backlog_drain_seconds"] = 5.9
    elif case == "accepted_identity_partition":
        replacements = {
            old: f"00000000-0000-4000-9000-{index:012x}"
            for index, old in enumerate(burst["accepted_run_ids"], start=1)
        }
        burst["accepted_run_ids"] = [replacements[run_id] for run_id in burst["accepted_run_ids"]]
        for claim in burst["worker_participation"]["claims"]:
            claim["run_id"] = replacements[claim["run_id"]]
        for event in burst["timing"]["durable_utc"]["claim_or_yield_events"]:
            event["run_id"] = replacements[event["run_id"]]
        for run in burst["timing"]["durable_utc"]["run_first_claim_to_finish"]:
            run["run_id"] = replacements[run["run_id"]]
    elif case == "accepted_run_duplicate":
        burst["accepted_run_ids"][-1] = burst["accepted_run_ids"][0]
    elif case == "measurement_run_duplicate":
        burst["run_ids"][-1] = burst["run_ids"][0]
    elif case == "scheduling_run_duplicate":
        burst["cooperative_scheduling"]["per_run"][-1]["run_id"] = burst["cooperative_scheduling"][
            "per_run"
        ][0]["run_id"]
    elif case == "scheduling_run_identity":
        burst["cooperative_scheduling"]["per_run"][0]["run_id"] = (
            "00000000-0000-4000-a000-000000000001"
        )
    elif case == "malformed_run_identity":
        burst["run_ids"][0] = "not-a-run-uuid"
    elif case == "foreign_timing_run":
        burst["timing"]["durable_utc"]["claim_or_yield_events"][0]["run_id"] = "foreign"
    elif case == "untyped_participation_claim":
        burst["timing"]["durable_utc"]["claim_or_yield_events"][0]["event_type"] = "run_yielded"
    elif case == "run_duration":
        burst["timing"]["durable_utc"]["run_first_claim_to_finish"][0]["duration_seconds"] = 3.9
    elif case == "durable_duration":
        burst["timing"]["durable_seconds"]["backlog_ready_to_first_claim"] = 1.0
    else:  # pragma: no cover - parameter contract
        raise AssertionError(case)

    with pytest.raises(script.QualificationFailure, match=match):
        script.validate_child_evidence(
            child,
            expected_commit="d" * 40,
            expected_hashes=_expected_hashes(),
            expected_order="single_then_multi",
        )


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("event_deletion", "event cardinality drift"),
        ("event_duplicate", "event cardinality drift"),
        ("terminal_replacement", "escaped the admitted set"),
        ("high_run_duplicate", "high Run IDs were not unique"),
        ("event_id_duplicate", "event IDs were not unique"),
        ("event_role", "event role drift"),
        ("terminal_model", "terminal Run Model role drift"),
        ("observation_model", "observed high-volume Model role drift"),
        ("data_model_binding", "did not match the primary Mock Model"),
        ("event_order", "events were not ordered"),
        ("equal_timestamp", "fairness ordering failed"),
        ("claim_boolean", "claim boolean drift"),
        ("incomplete_count", "incomplete count drift"),
    ],
)
def test_fairness_validator_recomputes_raw_identity_and_ordering(case: str, match: str) -> None:
    child = _happy_child()
    fairness = child["fairness"]
    ordering = fairness["ordering_evidence"]
    events = ordering["ordered_events"]
    if case == "event_deletion":
        low_run_id = fairness["low_volume_run_id"]
        ordering["ordered_events"].remove(
            next(
                event
                for event in events
                if event["run_id"] == low_run_id and event["event_type"] == "run_claimed"
            )
        )
    elif case == "event_duplicate":
        duplicate = copy.deepcopy(events[0])
        duplicate["event_id"] = "30000000-0000-4000-8000-00000000ffff"
        events.insert(1, duplicate)
    elif case == "terminal_replacement":
        fairness["terminal_runs"][0]["id"] = "40000000-0000-4000-8000-000000000001"
    elif case == "high_run_duplicate":
        fairness["high_volume_run_ids"][-1] = fairness["high_volume_run_ids"][0]
    elif case == "event_id_duplicate":
        events[-1]["event_id"] = events[0]["event_id"]
    elif case == "event_role":
        low_run_id = fairness["low_volume_run_id"]
        next(event for event in events if event["run_id"] == low_run_id)["role"] = "high_volume"
    elif case == "terminal_model":
        fairness["terminal_runs"][-1]["model_id"] = fairness["high_volume_model_id"]
    elif case == "observation_model":
        ordering["observation"]["high_runs"][0]["model_id"] = fairness["low_volume_model_id"]
    elif case == "data_model_binding":
        replacement = "10000000-0000-4000-8000-000000000003"
        old = fairness["high_volume_model_id"]
        fairness["high_volume_model_id"] = replacement
        for run in fairness["terminal_runs"]:
            if run["model_id"] == old:
                run["model_id"] = replacement
        for run in ordering["observation"]["high_runs"]:
            run["model_id"] = replacement
    elif case == "event_order":
        events[0], events[1] = events[1], events[0]
    elif case == "equal_timestamp":
        low_run_id = fairness["low_volume_run_id"]
        for event in events:
            if event["run_id"] == low_run_id and event["event_type"] == "run_claimed":
                event["occurred_at"] = "2026-08-28T00:00:05.700000Z"
        events.sort(
            key=lambda event: (
                script.parse_datetime(event["occurred_at"]),
                event["event_id"],
            )
        )
        ordering["low_claim_before_high_backlog_drained"] = False
    elif case == "claim_boolean":
        ordering["low_volume_claim_observed"] = False
    elif case == "incomplete_count":
        ordering["high_volume_incomplete_at_low_slice"] = 2
    else:  # pragma: no cover - parameter contract
        raise AssertionError(case)

    with pytest.raises(script.QualificationFailure, match=match):
        script.validate_child_evidence(
            child,
            expected_commit="d" * 40,
            expected_hashes=_expected_hashes(),
            expected_order="single_then_multi",
        )


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda value: value.update(status="failed"), "status"),
        (lambda value: value["repository"].update(dirty=True), "dirty"),
        (lambda value: value["configuration"].update(worker_poll_seconds=0.5), "configuration"),
        (
            lambda value: value["repository"].update(capacity_script_sha256="0" * 64),
            "capacity_script_sha256",
        ),
        (lambda value: value["measurements"].pop(), "measurement count"),
        (
            lambda value: value["measurements"].__setitem__(
                1, copy.deepcopy(value["measurements"][0])
            ),
            "measurement execution order",
        ),
        (lambda value: value["faults"].pop(), "fault scenario count"),
        (lambda value: value["cleanup"].update(status="failed"), "cleanup"),
        (
            lambda value: value["cleanup"]["remaining_containers"].append("leftover"),
            "remaining_containers",
        ),
    ],
)
def test_child_validator_fails_closed_on_status_profile_cells_faults_and_cleanup(
    mutation: Any,
    match: str,
) -> None:
    child = _happy_child()
    mutation(child)

    with pytest.raises(script.QualificationFailure, match=match):
        script.validate_child_evidence(
            child,
            expected_commit="d" * 40,
            expected_hashes=_expected_hashes(),
            expected_order="single_then_multi",
        )


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("schema", "schema"),
        ("profile", "qualification_profile"),
        ("runs", "runs"),
        ("responses", "responses"),
        ("question_executions", "question_executions"),
        ("reservations", "reservations"),
        ("reservation_states", "terminal-state"),
        ("audit", "provider_attempt_reserved"),
        ("image_status", "image cleanup"),
        ("image_candidates", "project_image_candidates"),
        ("images_removed", "removed_project_images"),
        ("images_retained", "retained_shared_project_images"),
        ("images_remaining", "remaining_project_images"),
        ("boolean_image_count", "not an integer"),
    ],
)
def test_child_validator_requires_v2_profile_reconciliation_and_exact_image_cleanup(
    case: str,
    match: str,
) -> None:
    child = _happy_child()
    database = child["reconciliation"]["database"]
    cleanup = child["cleanup"]
    if case == "schema":
        child["schema_version"] = "llmbenchlab-phase2-capacity-evidence-v1"
    elif case == "profile":
        child["configuration"]["qualification_profile"] = "capacity-v1"
    elif case == "runs":
        database["runs"] = 21
    elif case == "responses":
        database["responses"] = 329
    elif case == "question_executions":
        database["question_executions"] = 329
    elif case == "reservations":
        database["reservations"] = 330
    elif case == "reservation_states":
        database["reservation_states"] = {"settled_actual": 329, "settled_conservative": 2}
    elif case == "audit":
        database["audit_event_types"]["provider_attempt_reserved"] = 330
    elif case == "image_status":
        cleanup["image_cleanup_status"] = "failed"
    elif case == "image_candidates":
        cleanup["project_image_candidates"] = 0
    elif case == "images_removed":
        cleanup["removed_project_images"] = 0
    elif case == "images_retained":
        cleanup["retained_shared_project_images"] = 1
    elif case == "images_remaining":
        cleanup["remaining_project_images"] = 1
    elif case == "boolean_image_count":
        cleanup["project_image_candidates"] = True
    else:  # pragma: no cover - parameter contract
        raise AssertionError(case)

    with pytest.raises(script.QualificationFailure, match=match):
        script.validate_child_evidence(
            child,
            expected_commit="d" * 40,
            expected_hashes=_expected_hashes(),
            expected_order="single_then_multi",
        )


def test_child_validator_rejects_commit_environment_configuration_and_data_drift() -> None:
    baseline = script.validate_child_evidence(
        _happy_child(),
        expected_commit="d" * 40,
        expected_hashes=_expected_hashes(),
        expected_order="single_then_multi",
    )

    commit_drift = _happy_child(commit="0" * 40)
    with pytest.raises(script.QualificationFailure, match="commit"):
        script.validate_child_evidence(
            commit_drift,
            expected_commit="d" * 40,
            expected_hashes=_expected_hashes(),
            expected_order="single_then_multi",
        )

    environment_drift = _happy_child()
    environment_drift["environment"]["host"]["os_release"] = "different"
    with pytest.raises(script.QualificationFailure, match="environment"):
        script.validate_child_evidence(
            environment_drift,
            expected_commit="d" * 40,
            expected_hashes=_expected_hashes(),
            expected_order="single_then_multi",
            expected_environment_fingerprint=baseline["environment_fingerprint"],
        )

    configuration_drift = _happy_child()
    configuration_drift["configuration"]["unapproved_extra"] = "different"
    with pytest.raises(script.QualificationFailure, match="configuration"):
        script.validate_child_evidence(
            configuration_drift,
            expected_commit="d" * 40,
            expected_hashes=_expected_hashes(),
            expected_order="single_then_multi",
            expected_configuration_fingerprint=baseline["configuration_fingerprint"],
        )

    data_drift = _happy_child()
    data_drift["data"]["benchmark"]["dataset_hash"] = "0" * 64
    with pytest.raises(script.QualificationFailure, match="dataset_hash"):
        script.validate_child_evidence(
            data_drift,
            expected_commit="d" * 40,
            expected_hashes=_expected_hashes(),
            expected_order="single_then_multi",
            expected_data_fingerprint=baseline["data_fingerprint"],
        )


def test_child_validator_requires_qps_from_serialized_wall_duration() -> None:
    child = _happy_child()
    multi = next(
        measurement
        for measurement in child["measurements"]
        if measurement["name"] == "configured_multi_worker_baseline"
    )
    multi["wall_duration_seconds"] = 5.188009
    multi["throughput"]["questions_per_second"] = round(60 / 5.188009, 6)

    script.validate_child_evidence(
        child,
        expected_commit="d" * 40,
        expected_hashes=_expected_hashes(),
        expected_order="single_then_multi",
    )

    multi["throughput"]["questions_per_second"] -= 0.000001
    with pytest.raises(script.QualificationFailure, match="throughput disagrees"):
        script.validate_child_evidence(
            child,
            expected_commit="d" * 40,
            expected_hashes=_expected_hashes(),
            expected_order="single_then_multi",
        )


def _evaluated_trial() -> dict[str, Any]:
    measurements: dict[str, Any] = {}
    for name in script.MEASUREMENT_NAMES:
        latency: dict[str, Any] = {}
        for dimension in ("queue", "execution", "end_to_end"):
            objective = script.SLO_THRESHOLDS["latency_p95_seconds"][name][dimension]
            latency[dimension] = {"p95": objective, "samples": [objective] * 4}
        measurements[name] = {
            "questions_per_second": script.SLO_THRESHOLDS["throughput"][name][
                "lcb_questions_per_second"
            ],
            "latency_seconds": latency,
            "completed_questions": 60,
            "question_errors": 0,
            "provider_attempts_per_question": 1.0,
            "question_latency_ms": {
                "p95": 1.0,
                "samples": [1.0] * 60,
            },
        }
    for name in script.BURST_MEASUREMENT_NAMES:
        measurements[name]["backlog_drain_seconds"] = script.SLO_THRESHOLDS[
            "backlog_drain_seconds"
        ][name]
    return {
        "environment": {"postgres_max_connections": 100},
        "hard_invariants": {"all_child_contracts": True},
        "metrics": {
            "measurements": measurements,
            "faults": {
                "lease_kill_fence_to_reclaim_seconds": script.SLO_THRESHOLDS[
                    "lease_kill_fence_to_reclaim_seconds"
                ],
                "lease_expiry_to_reclaim_seconds": script.SLO_THRESHOLDS[
                    "lease_expiry_to_reclaim_seconds"
                ],
                "redis_run_created_to_claim_seconds": script.SLO_THRESHOLDS[
                    "redis_run_created_to_claim_seconds"
                ],
            },
        },
    }


def _validated_child() -> dict[str, Any]:
    evaluated = _evaluated_trial()
    return {
        "environment": evaluated["environment"],
        "environment_fingerprint": "1" * 64,
        "configuration_fingerprint": "2" * 64,
        "data_fingerprint": "3" * 64,
        "metrics": evaluated["metrics"],
        "hard_invariants": evaluated["hard_invariants"],
    }


def _suite_arguments() -> Any:
    return script.parse_arguments(
        [
            "--measured-trials",
            "5",
            "--seed",
            "20260828",
            "--trial-timeout-seconds",
            "300",
            "--artifacts-root",
            "artifacts",
        ]
    )


def _install_successful_suite_fakes(
    monkeypatch: pytest.MonkeyPatch,
    repository_root: Path,
) -> dict[str, Any]:
    commit = "d" * 40
    hashes = _expected_hashes() | {"slo_script_sha256": "e" * 64}
    state: dict[str, Any] = {
        "commands": [],
        "environments": [],
        "child_count": 0,
        "validator_count": 0,
    }
    monkeypatch.setattr(
        script,
        "_ensure_internal_artifact_root",
        lambda _root, _value: repository_root / "artifacts",
    )
    monkeypatch.setattr(script, "_repository_state", lambda _root: (commit, []))
    monkeypatch.setattr(script, "_expected_hashes", lambda _root: copy.deepcopy(hashes))

    def run_child(
        command: list[str],
        *,
        environment: dict[str, str],
        **_kwargs: Any,
    ) -> int:
        state["commands"].append(list(command))
        state["environments"].append(dict(environment))
        return 0

    def child_evidence(
        trial_root: Path,
        root: Path,
    ) -> tuple[Path, dict[str, Any], str]:
        state["child_count"] += 1
        child_path = trial_root / "child" / "evidence.json"
        command = state["commands"][-1]
        order = command[command.index("--measurement-order") + 1]
        child = _happy_child(order)
        child["project_name"] = f"llmbenchlab-p2-{state['child_count']:012x}"
        child["artifacts"] = child_path.relative_to(root).as_posix()
        return child_path, child, f"{state['child_count']:064x}"

    def validate_child(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        state["validator_count"] += 1
        return copy.deepcopy(_validated_child())

    monkeypatch.setattr(script, "_run_child", run_child)
    monkeypatch.setattr(script, "_single_child_evidence", child_evidence)
    monkeypatch.setattr(script, "validate_child_evidence", validate_child)
    return state


def _retained_failure(exc: script.QualificationFailure) -> dict[str, Any]:
    assert exc.evidence_path is not None
    return json.loads(exc.evidence_path.read_text(encoding="utf-8"))


def test_suite_slo_thresholds_pass_at_exact_boundaries_and_use_correct_directions() -> None:
    trials = [_evaluated_trial() for _ in range(5)]

    evaluation = script.evaluate_suite(trials)

    assert evaluation["status"] == "passed"
    assert len(evaluation["slo_results"]) == 23
    assert all(result["passed"] for result in evaluation["slo_results"])
    multi_lcb = script.SLO_THRESHOLDS["throughput"]["configured_multi_worker_baseline"][
        "lcb_questions_per_second"
    ]
    assert evaluation["capacity_model"]["safe_question_arrival_rate_per_second"] == pytest.approx(
        0.70 * multi_lcb
    )
    assert evaluation["capacity_model"]["inputs"]["database_jitter_budget_seconds"] == 1.0
    assert evaluation["capacity_model"]["inputs"]["mock_slice_service_budget_seconds"] == 0.4
    takeover = evaluation["capacity_model"]["lease_takeover_model"]
    assert takeover["expiry_to_claim_upper_bound_seconds"] == 6.0
    assert takeover["kill_fence_to_claim_upper_bound_seconds"] == 36.0
    assert takeover["expiry_model_within_objective"] is True
    assert takeover["kill_fence_model_within_objective"] is True

    throughput_failure = copy.deepcopy(trials)
    throughput_failure[0]["metrics"]["measurements"]["single_worker_reference"][
        "questions_per_second"
    ] = 1.0
    failed = script.evaluate_suite(throughput_failure)
    result = next(
        item
        for item in failed["slo_results"]
        if item["name"] == "single_worker_reference.throughput"
    )
    assert failed["status"] == "failed"
    assert result["passed"] is False

    assert set(evaluation["statistics"]["backlog_drain_seconds"]) == set(
        script.BURST_MEASUREMENT_NAMES
    )
    assert set(evaluation["statistics"]["question_error_zero_event_bounds"]) == set(
        script.MEASUREMENT_NAMES
    )

    latency_failure = copy.deepcopy(trials)
    latency_failure[0]["metrics"]["measurements"]["configured_multi_worker_baseline"][
        "latency_seconds"
    ]["queue"]["p95"] += 0.000001
    failed = script.evaluate_suite(latency_failure)
    result = next(
        item
        for item in failed["slo_results"]
        if item["name"] == "configured_multi_worker_baseline.queue_p95"
    )
    assert result["passed"] is False


@pytest.mark.parametrize("name", script.BURST_MEASUREMENT_NAMES)
def test_each_burst_is_an_independent_throughput_and_drain_and_gate(name: str) -> None:
    trials = [_evaluated_trial() for _ in range(5)]
    throughput_failure = copy.deepcopy(trials)
    throughput_failure[0]["metrics"]["measurements"][name]["questions_per_second"] = 1.0
    throughput_evaluation = script.evaluate_suite(throughput_failure)
    throughput_result = next(
        item
        for item in throughput_evaluation["slo_results"]
        if item["name"] == f"{name}.throughput"
    )
    assert throughput_evaluation["status"] == "failed"
    assert throughput_result["passed"] is False

    drain_failure = copy.deepcopy(trials)
    drain_failure[0]["metrics"]["measurements"][name]["backlog_drain_seconds"] += 0.000001
    drain_evaluation = script.evaluate_suite(drain_failure)
    drain_result = next(
        item for item in drain_evaluation["slo_results"] if item["name"] == f"{name}.backlog_drain"
    )
    assert drain_evaluation["status"] == "failed"
    assert drain_result["passed"] is False


@pytest.mark.parametrize("name", script.BURST_MEASUREMENT_NAMES)
@pytest.mark.parametrize("dimension", ("queue", "execution", "end_to_end"))
def test_each_burst_latency_dimension_is_an_independent_and_gate(
    name: str,
    dimension: str,
) -> None:
    trials = [_evaluated_trial() for _ in range(5)]
    trials[2]["metrics"]["measurements"][name]["latency_seconds"][dimension]["p95"] += 0.000001

    evaluation = script.evaluate_suite(trials)
    result = next(
        item for item in evaluation["slo_results"] if item["name"] == f"{name}.{dimension}_p95"
    )

    assert evaluation["status"] == "failed"
    assert result["passed"] is False


def test_failed_trial_hard_invariant_cannot_be_averaged_into_a_pass() -> None:
    trials = [_evaluated_trial() for _ in range(5)]
    trials[2]["hard_invariants"]["all_child_contracts"] = False

    with pytest.raises(script.QualificationFailure, match="hard invariant"):
        script.evaluate_suite(trials)


def test_validator_and_trial_reference_are_strict_allowlists_without_secret_canary(
    tmp_path: Path,
) -> None:
    canary = "sk-slo-secret-canary-must-not-survive"
    child = _happy_child()
    child["commands"] = [{"stdout": canary, "authorization": f"Bearer {canary}"}]
    child["measurements"][0]["raw_debug_canary"] = canary
    child["environment"]["container_limits"]["api"][0]["container_id"] = canary
    for name in script.BURST_MEASUREMENT_NAMES:
        burst = _burst_measurement(child, name)
        burst["worker_participation"]["validated_workers"][0]["container_id"] = "9" * 64
        burst["worker_state_after_restart"][0]["id"] = "9" * 64
    validated = script.validate_child_evidence(
        child,
        expected_commit="d" * 40,
        expected_hashes=_expected_hashes(),
        expected_order="single_then_multi",
    )
    evidence_path = tmp_path / "evidence.json"
    _write_json(evidence_path, child)
    reference = script._trial_reference(
        role="measured",
        index=1,
        order="single_then_multi",
        evidence_path=evidence_path,
        evidence_sha256="0" * 64,
        repository_root=tmp_path,
        validated=validated,
    )

    assert set(reference) == {
        "role",
        "index",
        "measurement_order",
        "evidence_path",
        "evidence_sha256",
        "status",
        "environment_fingerprint",
        "configuration_fingerprint",
        "data_fingerprint",
        "metrics",
        "hard_invariants",
    }
    assert canary not in json.dumps(validated, sort_keys=True)
    assert canary not in json.dumps(reference, sort_keys=True)
    serialized_metrics = json.dumps(validated["metrics"], sort_keys=True)
    assert '"run_id"' not in serialized_metrics
    assert '"worker_id"' not in serialized_metrics
    assert '"container_id"' not in serialized_metrics
    assert "9" * 64 not in serialized_metrics


def test_single_child_evidence_rejects_missing_and_multiple_files(tmp_path: Path) -> None:
    trial_root = tmp_path / "trial"
    trial_root.mkdir()
    with pytest.raises(script.QualificationFailure, match="exactly one"):
        script._single_child_evidence(trial_root, tmp_path)

    first = trial_root / "one" / "evidence.json"
    second = trial_root / "two" / "evidence.json"
    first.parent.mkdir()
    second.parent.mkdir()
    _write_json(first, {"status": "passed"})
    _write_json(second, {"status": "passed"})
    with pytest.raises(script.QualificationFailure, match="exactly one"):
        script._single_child_evidence(trial_root, tmp_path)


def test_child_process_uses_own_group_and_timeout_gets_full_cleanup_grace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    transitions: list[object] = []
    popen_kwargs: list[dict[str, Any]] = []
    group_signals: list[tuple[int, int]] = []

    class NonzeroProcess:
        pid = 41001

        def poll(self) -> int:
            return 7

    nonzero = NonzeroProcess()

    def spawn_nonzero(*_args: Any, **kwargs: Any) -> NonzeroProcess:
        popen_kwargs.append(kwargs)
        return nonzero

    monkeypatch.setattr(script.subprocess, "Popen", spawn_nonzero)
    assert (
        script._run_child(
            ["fake-capacity"],
            repository_root=tmp_path,
            environment={},
            timeout_seconds=300,
            set_current_child=transitions.append,
            termination_requested=lambda: False,
        )
        == 7
    )
    assert popen_kwargs[0]["start_new_session"] is True
    assert transitions == [nonzero, None]

    class TimeoutProcess:
        pid = 41002

        def poll(self) -> None:
            return None

        def wait(self, timeout: int) -> int:
            assert script.CHILD_CLEANUP_GRACE_SECONDS == 420
            assert timeout == script.CHILD_CLEANUP_GRACE_SECONDS
            return 1

    timed_out = TimeoutProcess()
    transitions.clear()
    monkeypatch.setattr(script.subprocess, "Popen", lambda *_args, **_kwargs: timed_out)
    monkeypatch.setattr(
        script.os,
        "killpg",
        lambda process_group, received: group_signals.append((process_group, received)),
    )
    monotonic_values = iter((0.0, 301.0))
    monkeypatch.setattr(script.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    with pytest.raises(script.QualificationFailure, match="timed out"):
        script._run_child(
            ["fake-capacity"],
            repository_root=tmp_path,
            environment={},
            timeout_seconds=300,
            set_current_child=transitions.append,
            termination_requested=lambda: False,
        )
    assert group_signals == [(timed_out.pid, signal.SIGTERM)]
    assert transitions == [timed_out, None]


def test_run_suite_executes_one_warmup_then_five_measured_trials_and_scrubs_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _install_successful_suite_fakes(monkeypatch, tmp_path)
    for key in script.PROVIDER_CREDENTIAL_ENV_KEYS:
        monkeypatch.setenv(key, f"secret-{key}")

    aggregate, evidence_path = script.run_suite(_suite_arguments(), tmp_path)

    measured_orders = script.balanced_measurement_orders(seed=20260828, measured_trials=5)
    expected_orders = ["single_then_multi", *measured_orders]
    observed_orders = [
        command[command.index("--measurement-order") + 1] for command in state["commands"]
    ]
    assert observed_orders == expected_orders
    assert state["child_count"] == 6
    assert state["validator_count"] == 6
    assert [(trial["role"], trial["index"]) for trial in aggregate["trials"]] == [
        ("warmup", 0),
        ("measured", 1),
        ("measured", 2),
        ("measured", 3),
        ("measured", 4),
        ("measured", 5),
    ]
    assert [trial["measurement_order"] for trial in aggregate["trials"]] == expected_orders
    assert aggregate["status"] == "passed"
    assert aggregate["experiment"]["discarded_trials"] == 0
    assert evidence_path.is_file()
    assert all(
        key not in environment
        for environment in state["environments"]
        for key in script.PROVIDER_CREDENTIAL_ENV_KEYS
    )


def test_run_suite_retains_failed_trial_reference_when_child_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _install_successful_suite_fakes(monkeypatch, tmp_path)

    def fail_child(
        command: list[str],
        *,
        environment: dict[str, str],
        **_kwargs: Any,
    ) -> int:
        state["commands"].append(list(command))
        state["environments"].append(dict(environment))
        return 17

    monkeypatch.setattr(script, "_run_child", fail_child)

    with pytest.raises(script.QualificationFailure, match="exited unsuccessfully") as caught:
        script.run_suite(_suite_arguments(), tmp_path)

    evidence = _retained_failure(caught.value)
    assert state["child_count"] == 1
    assert evidence["status"] == "failed"
    assert evidence["failure"]["type"] == "QualificationFailure"
    assert len(evidence["trials"]) == 1
    trial = evidence["trials"][0]
    assert trial["role"] == "warmup"
    assert trial["index"] == 0
    assert trial["measurement_order"] == "single_then_multi"
    assert trial["evidence_path"].endswith("/child/evidence.json")
    assert trial["evidence_sha256"] == "1".zfill(64)
    assert trial["status"] == "failed"


def test_run_suite_retains_failed_trial_reference_when_child_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _install_successful_suite_fakes(monkeypatch, tmp_path)

    def reject_child(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        state["validator_count"] += 1
        raise script.QualificationFailure("synthetic child validation failure")

    monkeypatch.setattr(script, "validate_child_evidence", reject_child)

    with pytest.raises(script.QualificationFailure, match="synthetic child validation") as caught:
        script.run_suite(_suite_arguments(), tmp_path)

    evidence = _retained_failure(caught.value)
    assert state["child_count"] == 1
    assert state["validator_count"] == 1
    assert evidence["status"] == "failed"
    assert len(evidence["trials"]) == 1
    trial = evidence["trials"][0]
    assert trial["role"] == "warmup"
    assert trial["index"] == 0
    assert trial["measurement_order"] == "single_then_multi"
    assert trial["evidence_path"].endswith("/child/evidence.json")
    assert trial["evidence_sha256"] == "1".zfill(64)
    assert trial["status"] == "failed"


def test_run_suite_dirty_preflight_fails_closed_and_retains_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_successful_suite_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        script,
        "_repository_state",
        lambda _root: ("d" * 40, [" M scripts/phase2_slo.py"]),
    )

    with pytest.raises(script.QualificationFailure, match="clean Git worktree") as caught:
        script.run_suite(_suite_arguments(), tmp_path)

    evidence = _retained_failure(caught.value)
    assert evidence["repository"]["dirty"] is True
    assert evidence["status"] == "failed"
    assert evidence["trials"] == []
    assert evidence["failure"] == {
        "type": "QualificationFailure",
        "message": "formal qualification requires a clean Git worktree",
    }


def test_run_suite_final_source_drift_fails_after_all_trials_and_retains_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_successful_suite_fakes(monkeypatch, tmp_path)
    initial_hashes = _expected_hashes() | {"slo_script_sha256": "e" * 64}
    final_hashes = initial_hashes | {"slo_script_sha256": "f" * 64}
    hash_snapshots = iter((initial_hashes, final_hashes))
    monkeypatch.setattr(script, "_expected_hashes", lambda _root: next(hash_snapshots))

    with pytest.raises(script.QualificationFailure, match="source hashes changed") as caught:
        script.run_suite(_suite_arguments(), tmp_path)

    evidence = _retained_failure(caught.value)
    assert evidence["status"] == "failed"
    assert len(evidence["trials"]) == 6
    assert all(trial["status"] == "passed" for trial in evidence["trials"])
    assert evidence["failure"] == {
        "type": "QualificationFailure",
        "message": "qualification source hashes changed",
    }


def test_git_commands_scrub_repository_override_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    override_keys = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    )
    for key in override_keys:
        monkeypatch.setenv(key, f"untrusted-{key}")
    monkeypatch.setenv("SLO_SAFE_SENTINEL", "preserved")
    captured: dict[str, Any] = {}

    class Completed:
        returncode = 0
        stdout = "result\n"

    def run(*_args: Any, **kwargs: Any) -> Completed:
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(script.subprocess, "run", run)

    assert script._run_git(tmp_path, "rev-parse", "HEAD") == "result"
    environment = captured["env"]
    assert environment["SLO_SAFE_SENTINEL"] == "preserved"
    assert all(key not in environment for key in override_keys)


def test_child_environment_is_allowlisted_and_removes_git_and_provider_overrides() -> None:
    source = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/safe-home",
        "LC_TEST": "preserved",
        "PYTHONPATH": "/untrusted/imports",
        "OPENAI_API_KEY": "secret-provider-key",
        "GIT_DIR": "/untrusted/repository",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/untrusted/hooks",
    }

    assert script._child_environment(source) == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/safe-home",
        "LC_TEST": "preserved",
    }


def test_repository_state_rejects_git_root_override_or_nested_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run_git(_root: Path, *arguments: str) -> str:
        calls.append(arguments)
        if arguments == ("rev-parse", "--show-toplevel"):
            return str(tmp_path.parent)
        if arguments == ("rev-parse", "HEAD"):
            return "d" * 40
        if arguments[:1] == ("status",):
            return ""
        raise AssertionError(f"unexpected Git invocation: {arguments}")

    monkeypatch.setattr(script, "_run_git", run_git)

    with pytest.raises(script.QualificationFailure, match="repository root"):
        script._repository_state(tmp_path)
    assert ("rev-parse", "--show-toplevel") in calls


def test_fixed_child_command_uses_formal_timing_and_repository_internal_artifacts(
    tmp_path: Path,
) -> None:
    trial_root = tmp_path / "artifacts" / "trial"
    command = script._child_command(tmp_path, trial_root, "multi_then_single")
    joined = " ".join(command)

    assert "--lease-seconds 30" in joined
    assert "--heartbeat-seconds 10" in joined
    assert "--worker-poll-seconds 1" in joined
    assert "--mock-delay-seconds 0.08" in joined
    assert "--measurement-order multi_then_single" in joined
    assert command.count("--qualification-profile") == 1
    profile_index = command.index("--qualification-profile")
    assert command[profile_index + 1] == "P2-local-control-plane-v2"
    assert "capacity-v1" not in command
    assert str(trial_root.relative_to(tmp_path)) in command
    assert not any(key in joined for key in script.PROVIDER_CREDENTIAL_ENV_KEYS)


def test_cli_self_check_executes_without_git_docker_or_network(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert script.main(["--self-check-only"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "passed"
    assert payload["profile"] == script.PROFILE_NAME
