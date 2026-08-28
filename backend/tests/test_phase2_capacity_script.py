"""Offline unit checks for the dependency-free Phase 2 capacity harness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_CAPACITY_SCRIPT = _REPOSITORY_ROOT / "scripts" / "phase2_capacity.py"
_ACCEPTANCE_SCRIPT = _REPOSITORY_ROOT / "scripts" / "phase2_acceptance.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase2_capacity_script", _CAPACITY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


def _valid_reconciliation_snapshot(
    *, runs_per_phase: int = 4, backlog_limit: int = 4
) -> dict[str, object]:
    expected_counts, expected_states, expected_audit_counts = script.reconciliation_expectations(
        runs_per_phase=runs_per_phase,
        backlog_limit=backlog_limit,
    )
    snapshot: dict[str, object] = {field: 0 for field in script.RECONCILIATION_ZERO_FIELDS}
    snapshot.update(expected_counts)
    snapshot.update(
        {
            "reservation_states": expected_states,
            "audit_events": 1229,
            "audit_event_types": {
                **expected_audit_counts,
                "question_evidence_persisted": expected_counts["responses"],
            },
        }
    )
    return snapshot


def test_percentile_uses_deterministic_linear_interpolation() -> None:
    values = [4.0, 1.0, 3.0, 2.0]

    assert script.percentile(values, 0) == 1.0
    assert script.percentile(values, 50) == 2.5
    assert script.percentile(values, 95) == pytest.approx(3.85)
    assert script.percentile(values, 99) == pytest.approx(3.97)
    assert script.percentile(values, 100) == 4.0

    with pytest.raises(ValueError, match="at least one"):
        script.percentile([], 50)
    with pytest.raises(ValueError, match="between 0 and 100"):
        script.percentile(values, 101)


def test_distribution_reports_required_capacity_percentiles() -> None:
    summary = script.distribution([1, 2, 3, 4])

    assert summary == {
        "count": 4,
        "min": 1.0,
        "mean": 2.5,
        "p50": 2.5,
        "p95": 3.85,
        "p99": 3.97,
        "max": 4.0,
    }
    assert script.distribution([]) == {
        "count": 0,
        "min": None,
        "mean": None,
        "p50": None,
        "p95": None,
        "p99": None,
        "max": None,
    }


def test_capacity_cli_defaults_are_bounded_and_require_two_workers() -> None:
    args = script.parse_arguments([])

    assert args.workers == 2
    assert args.runs_per_phase == 4
    assert args.backlog_limit == 4
    assert args.burst_runs == 6
    assert args.submit_concurrency == 6
    assert args.run_concurrency == 1
    assert 1 <= args.question_quantum < 15
    assert args.mock_delay_seconds == 0.08
    assert args.timeout_seconds == 180
    assert args.lease_seconds == 6
    assert args.heartbeat_seconds == 2
    assert args.worker_poll_seconds == 0.15
    assert args.measurement_order == "single_then_multi"

    with pytest.raises(ValueError, match="at least 2"):
        script.parse_arguments(["--workers", "1"])
    with pytest.raises(ValueError, match=r"between 0\.01 and 10"):
        script.parse_arguments(["--mock-delay-seconds", "0"])
    with pytest.raises(ValueError, match="between 1 and 4"):
        script.parse_arguments(["--run-concurrency", "5"])
    with pytest.raises(ValueError, match="strictly greater than --backlog-limit"):
        script.parse_arguments(["--backlog-limit", "4", "--burst-runs", "4"])
    with pytest.raises(ValueError, match="less than the 15-question demo Run"):
        script.parse_arguments(["--question-quantum", "15"])
    with pytest.raises(ValueError, match="between 3 and 3600"):
        script.parse_arguments(["--lease-seconds", "2"])
    with pytest.raises(ValueError, match="at most half"):
        script.parse_arguments(["--lease-seconds", "6", "--heartbeat-seconds", "4"])
    with pytest.raises(ValueError, match=r"between 0\.05 and 60"):
        script.parse_arguments(["--worker-poll-seconds", "0.01"])
    with pytest.raises(SystemExit):
        script.parse_arguments(["--measurement-order", "unknown"])


def test_demo_producer_retains_slo_identity_fields() -> None:
    harness = object.__new__(script.Phase2Acceptance)
    harness.project = "llmbenchlab-p2-123456789abc"
    payloads = iter(
        [
            {
                "id": "benchmark-id",
                "slug": "demo-general",
                "version": "1.0.0",
                "schema_version": "llmbenchlab-dataset-v1",
                "dataset_hash": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe",
                "question_count": 15,
            },
            {
                "id": "model-id",
                "provider_type": "mock",
                "enabled": True,
                "api_key_env": None,
            },
        ]
    )
    harness.http_json = lambda *_args, **_kwargs: {"payload": next(payloads)}

    produced = harness.initialize_demo()

    assert produced["benchmark"] == {
        "id": "benchmark-id",
        "slug": "demo-general",
        "version": "1.0.0",
        "schema_version": "llmbenchlab-dataset-v1",
        "dataset_hash": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe",
        "question_count": 15,
    }


def test_capacity_timing_args_override_compose_env_and_are_recorded() -> None:
    args = script.parse_arguments(
        [
            "--lease-seconds",
            "30",
            "--heartbeat-seconds",
            "10",
            "--worker-poll-seconds",
            "1",
            "--measurement-order",
            "multi_then_single",
        ]
    )
    harness = script.Phase2Capacity(_REPOSITORY_ROOT, args)
    try:
        assert harness.env["LLMBENCHLAB_COMPOSE_WORKER_LEASE_SECONDS"] == "30.0"
        assert harness.env["LLMBENCHLAB_COMPOSE_WORKER_HEARTBEAT_SECONDS"] == "10.0"
        assert harness.env["LLMBENCHLAB_COMPOSE_WORKER_POLL_SECONDS"] == "1.0"
        configuration = harness.evidence["configuration"]
        assert {
            key: configuration[key]
            for key in (
                "lease_seconds",
                "heartbeat_seconds",
                "worker_poll_seconds",
                "measurement_order",
                "database_pool_size",
                "database_max_overflow",
            )
        } == {
            "lease_seconds": 30.0,
            "heartbeat_seconds": 10.0,
            "worker_poll_seconds": 1.0,
            "measurement_order": "multi_then_single",
            "database_pool_size": 5,
            "database_max_overflow": 5,
        }
    finally:
        harness._credential_secret_dir.cleanup()


def test_capacity_self_review_reports_all_removed_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        script.Phase2Acceptance,
        "self_review",
        lambda _self: {"real_provider_credentials_removed": ["stale"]},
    )
    harness = object.__new__(script.Phase2Capacity)
    harness.evidence = {"repository": {}}
    harness.worker_count = 2
    harness.backlog_limit = 4
    harness.question_quantum = 5

    review = harness.self_review()

    assert review["real_provider_credentials_removed"] == [
        "OPENAI_API_KEY",
        "LLMBENCHLAB_DEMO_API_KEY",
        "LLMBENCHLAB_REAL_API_KEY",
        "TEST_PROVIDER_KEY",
    ]
    assert review["real_provider_credentials_removed"] == list(script.PROVIDER_CREDENTIAL_ENV_KEYS)


def test_utc_timing_helpers_require_typed_ordered_non_negative_facts() -> None:
    events = [
        {
            "id": "claim-later",
            "event_type": "run_claimed",
            "occurred_at": "2026-08-28T00:00:04Z",
        },
        {
            "id": "yield",
            "event_type": "run_yielded",
            "occurred_at": "2026-08-28T00:00:02Z",
        },
        {
            "id": "claim-first",
            "event_type": "run_claimed",
            "occurred_at": "2026-08-28T00:00:03Z",
        },
    ]

    claim = script.first_claim_at_or_after(events, "2026-08-28T00:00:02.500000Z")

    assert claim["id"] == "claim-first"
    assert (
        script.nonnegative_utc_elapsed_seconds(
            "2026-08-28T00:00:02.500000Z",
            claim["occurred_at"],
        )
        == 0.5
    )
    with pytest.raises(ValueError, match="no typed run_claimed"):
        script.first_claim_at_or_after(events, "2026-08-28T00:00:05Z")
    with pytest.raises(ValueError, match="finite and non-negative"):
        script.nonnegative_utc_elapsed_seconds(
            "2026-08-28T00:00:03Z",
            "2026-08-28T00:00:02Z",
        )


def test_capacity_policy_is_explicit_finite_and_uses_a_cooperative_quantum() -> None:
    policy = script.finite_capacity_policy(backlog_limit=4, question_quantum=5)

    assert policy["backlog_limit"] == 4
    assert policy["question_quantum"] == 5
    assert all(value is not None for value in policy.values())
    assert all(
        value > 0
        for key, value in policy.items()
        if key not in {"question_quantum", "backlog_limit"} and not key.endswith("_usd")
    )
    assert all(float(value) > 0 for key, value in policy.items() if key.endswith("_usd"))


def test_capacity_policy_is_applied_and_read_back_through_the_policy_api() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.backlog_limit = 4
    harness.question_quantum = 5
    harness.policy_document = None
    calls: list[tuple[str, str, dict[str, object] | None]] = []
    policy = script.finite_capacity_policy(backlog_limit=4, question_quantum=5)
    response = {
        "id": "policy-id",
        "version": 2,
        "policy_hash": "a" * 64,
        "is_active": True,
        "activated_at": "2026-08-28T00:00:00Z",
        **policy,
    }

    def fake_http_json(
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        accepted: set[int] | None = None,
        timeout: float = 5,
    ) -> dict[str, object]:
        del accepted, timeout
        calls.append((method, path, body))
        return {"payload": response}

    harness.http_json = fake_http_json
    harness.require = script.Phase2Acceptance.require.__get__(harness)

    evidence = harness.apply_capacity_policy()

    assert calls == [
        ("PUT", "/governance/policy", policy),
        ("GET", "/governance/policy", None),
    ]
    assert evidence["limits"] == policy
    assert harness.policy_document == response


def test_every_capacity_run_supplies_hard_budget_reservation_inputs() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.model_id = "high-volume-model"
    harness.benchmark_id = "demo-benchmark"
    harness.run_concurrency = 1
    captured: dict[str, object] = {}

    def fake_http_json(
        method: str,
        path: str,
        *,
        body: dict[str, object],
        accepted: set[int],
        timeout: float,
    ) -> dict[str, object]:
        captured.update(
            method=method,
            path=path,
            body=body,
            accepted=accepted,
            timeout=timeout,
        )
        return {"status_code": 202, "elapsed_seconds": 0.01, "payload": {"id": "run-1"}}

    harness.http_json = fake_http_json
    harness.require = script.Phase2Acceptance.require.__get__(harness)

    result = harness.create_capacity_run(model_id="low-volume-model")

    assert result["status_code"] == 202
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model_id"] == "low-volume-model"
    assert body["max_tokens"] == script.RUN_MAX_TOKENS
    assert body["input_token_reservation"] == script.RUN_INPUT_TOKEN_RESERVATION
    assert body["lifetime_request_budget"] > 0
    assert body["lifetime_token_budget"] >= (
        15 * (body["input_token_reservation"] + body["max_tokens"])
    )
    assert float(body["lifetime_cost_budget_usd"]) > 0


def test_submission_summary_preserves_exact_202_and_429_evidence() -> None:
    summary = script.summarize_submissions(
        [
            {"status_code": 202, "elapsed_seconds": 0.01, "payload": {"id": "run-a"}},
            {
                "status_code": 429,
                "elapsed_seconds": 0.02,
                "payload": {
                    "detail": {
                        "code": "run_backlog_full",
                        "message": "The managed Run backlog is at its configured limit.",
                        "limit": 1,
                    }
                },
            },
        ],
        duration_seconds=0.03,
    )

    assert summary["status_counts"] == {"202": 1, "429": 1}
    assert summary["accepted"] == [{"id": "run-a"}]
    assert summary["rejected"] == [
        {
            "status_code": 429,
            "payload": {
                "detail": {
                    "code": "run_backlog_full",
                    "message": "The managed Run backlog is at its configured limit.",
                    "limit": 1,
                }
            },
        }
    ]


def test_cooperative_scheduling_summary_requires_multiple_dispatches_and_yields() -> None:
    final_runs = [
        {"id": "run-a", "dispatch_count": 3},
        {"id": "run-b", "dispatch_count": 3},
    ]
    audit_events = {
        "run-a": [
            {"event_type": "run_claimed"},
            {"event_type": "run_yielded"},
            {"event_type": "run_claimed"},
            {"event_type": "run_yielded"},
            {"event_type": "run_claimed"},
        ],
        "run-b": [
            {"event_type": "run_claimed"},
            {"event_type": "run_yielded"},
            {"event_type": "run_claimed"},
            {"event_type": "run_yielded"},
            {"event_type": "run_claimed"},
        ],
    }

    summary = script.cooperative_scheduling_summary(final_runs, audit_events)

    assert summary["all_runs_dispatched_more_than_once"] is True
    assert summary["all_runs_yielded"] is True
    assert summary["claim_events"] == 6
    assert summary["cooperative_yield_events"] == 4
    assert summary["per_run"][0] == {
        "run_id": "run-a",
        "dispatch_count": 3,
        "claim_events": 3,
        "cooperative_yield_events": 2,
    }


def test_phase_result_adds_anonymous_raw_latency_samples_without_changing_summaries() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.responses = lambda _run_id: {
        "total": 2,
        "items": [
            {"id": "response-a", "latency_ms": 80.1234567, "error_type": None},
            {"id": "response-b", "latency_ms": 90, "error_type": None},
        ],
    }
    harness.run_audit_events = lambda _run_id: [
        {"event_type": "run_claimed"},
        {"event_type": "run_yielded"},
        {"event_type": "run_claimed"},
    ]
    harness.require = script.Phase2Acceptance.require.__get__(harness)
    final_runs = [
        {
            "id": "run-a",
            "status": "completed",
            "created_at": "2026-08-28T00:00:00Z",
            "started_at": "2026-08-28T00:00:01Z",
            "finished_at": "2026-08-28T00:00:03Z",
            "completed_questions": 2,
            "failed_attempt_count": 0,
            "attempt_count": 1,
            "dispatch_count": 2,
        }
    ]

    result = harness.phase_result(
        name="single_worker_reference",
        workers=1,
        submissions={
            "requested": 1,
            "accepted": [{"id": "run-a"}],
            "rejected": [],
            "status_counts": {"202": 1},
            "duration_seconds": 0.01,
            "request_latency_seconds": script.distribution([0.01]),
        },
        final_runs=final_runs,
        elapsed_seconds=3,
        metrics={},
        database_before={},
        database_after={
            "provider_reservations": 2,
            "settled_actual_reservations": 2,
            "settled_conservative_reservations": 0,
        },
        queue_before={},
        queue_after={},
    )

    assert result["latency_seconds"]["queue"] == {
        **script.distribution([1]),
        "samples": [1.0],
    }
    assert result["latency_seconds"]["execution"] == {
        **script.distribution([2]),
        "samples": [2.0],
    }
    assert result["latency_seconds"]["end_to_end"] == {
        **script.distribution([3]),
        "samples": [3.0],
    }
    assert result["question_latency_ms"] == {
        **script.distribution([80.1234567, 90]),
        "samples": [80.123457, 90.0],
    }
    assert "run-a" not in str(result["latency_seconds"])


@pytest.mark.parametrize(
    ("measurement_order", "expected"),
    [
        (
            "single_then_multi",
            [("single_worker_reference", 1), ("configured_multi_worker_baseline", 2)],
        ),
        (
            "multi_then_single",
            [("configured_multi_worker_baseline", 2), ("single_worker_reference", 1)],
        ),
    ],
)
def test_run_all_honors_measurement_order_without_renaming_cells(
    measurement_order: str,
    expected: list[tuple[str, int]],
) -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.evidence = {}
    harness.worker_count = 2
    harness.measurement_order = measurement_order
    harness.timeout_seconds = 1
    calls: list[tuple[str, int]] = []
    restored_worker_counts: list[int] = []
    harness.write_evidence = lambda: None
    harness.setup_stack = lambda: None
    harness.topology_and_data = lambda: None

    def measurement(name: str, workers: int) -> dict[str, object]:
        calls.append((name, workers))
        return {
            "run_ids": [f"{name}-run"],
            "throughput": {"questions_per_second": float(workers)},
        }

    harness.run_measurement_phase = measurement
    harness.scale_workers = lambda count: restored_worker_counts.append(count) or []
    harness.bounded_queue_burst = lambda: {"throughput": {"questions_per_second": 1.5}}
    harness.model_fairness_scenario = lambda: {
        "ordering_evidence": {"low_claim_before_high_backlog_drained": True}
    }
    harness.lease_expiry_fault = lambda: {}
    harness.redis_outage_fault = lambda: {}
    harness.duplicate_delivery_fault = lambda _run_id: {}
    harness.governance_reconciliation = lambda: {}
    harness.queue_pressure = lambda: {}
    harness.task_metrics = lambda: {}
    harness.wait_service_healthy = lambda *_args, **_kwargs: []

    harness.run_all()

    assert calls == expected
    assert restored_worker_counts == [2]
    assert harness.evidence["comparison"]["single_worker_questions_per_second"] == 1.0
    assert harness.evidence["comparison"]["multi_worker_questions_per_second"] == 2.0


def test_governance_reconciliation_sql_independently_rebuilds_both_projections() -> None:
    sql = script.GOVERNANCE_RECONCILIATION_SQL

    for scope_reference in (
        "reservation.global_scope_id",
        "reservation.provider_scope_id",
        "reservation.model_scope_id",
        "reservation.run_scope_id",
    ):
        assert scope_reference in sql
    for materialized_field in (
        "active_reservations",
        "reserved_requests",
        "consumed_requests",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_cost_usd",
        "consumed_input_tokens",
        "consumed_output_tokens",
        "consumed_cost_usd",
        "overdrawn",
    ):
        assert f"materialized.{materialized_field} IS DISTINCT FROM" in sql
    assert "derived_minutes AS MATERIALIZED" in sql
    assert "missing_scope_projection_rows" in sql
    assert "extra_scope_projection_rows" in sql
    assert "missing_minute_projection_rows" in sql
    assert "extra_minute_projection_rows" in sql
    assert "duplicate_response_questions" in sql
    assert "distinct_run_question_responses" in sql


def test_governance_reconciliation_executes_the_fixed_query_and_accepts_exact_snapshot() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.runs_per_phase = 4
    harness.backlog_limit = 4
    queries: list[str] = []

    def psql(query: str) -> SimpleNamespace:
        queries.append(query)
        return SimpleNamespace(stdout=json.dumps(_valid_reconciliation_snapshot()))

    harness.psql = psql

    snapshot = harness.governance_reconciliation()

    assert queries == [script.GOVERNANCE_RECONCILIATION_SQL]
    assert snapshot == _valid_reconciliation_snapshot()


def test_governance_reconciliation_derives_counts_from_nondefault_workload() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.runs_per_phase = 2
    harness.backlog_limit = 3
    expected = _valid_reconciliation_snapshot(runs_per_phase=2, backlog_limit=3)
    harness.psql = lambda _query: SimpleNamespace(stdout=json.dumps(expected))

    assert harness.governance_reconciliation() == expected
    assert expected["runs"] == 12
    assert expected["responses"] == 180
    assert expected["reservations"] == 181


def test_lease_fault_unpauses_worker_when_fence_snapshot_fails() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.worker_count = 2
    harness.timeout_seconds = 10
    harness.service_metas = lambda *_args, **_kwargs: [
        {"hostname": "worker-a", "id": "container-a"},
        {"hostname": "worker-b", "id": "container-b"},
    ]
    harness.create_capacity_run = lambda: {
        "status_code": 202,
        "payload": {"id": "run-id"},
    }
    harness.wait_for = lambda *_args, **_kwargs: {
        "status": "running",
        "lease_owner": "worker:worker-a:123:fixture",
        "lease_token": 1,
        "response_count": 0,
        "send_started_provider_attempts": 1,
    }
    commands: list[list[str]] = []

    def run_command(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    harness.run_command = run_command
    harness.db_run_snapshot = lambda _run_id: (_ for _ in ()).throw(
        script.AcceptanceFailure("fence snapshot failed")
    )

    with pytest.raises(script.AcceptanceFailure, match="fence snapshot failed"):
        harness.lease_expiry_fault()

    assert commands == [
        ["docker", "pause", "container-a"],
        ["docker", "unpause", "container-a"],
    ]


@pytest.mark.parametrize("field", script.RECONCILIATION_ZERO_FIELDS)
def test_governance_reconciliation_rejects_every_nonzero_integrity_field(field: str) -> None:
    snapshot = _valid_reconciliation_snapshot()
    snapshot[field] = 1

    with pytest.raises(script.AcceptanceFailure, match=field):
        script.validate_governance_reconciliation_snapshot(snapshot)


@pytest.mark.parametrize("field", script.EXPECTED_RECONCILIATION_COUNTS)
def test_governance_reconciliation_rejects_exact_count_drift(field: str) -> None:
    snapshot = _valid_reconciliation_snapshot()
    snapshot[field] = int(snapshot[field]) + 1

    with pytest.raises(script.AcceptanceFailure, match=field):
        script.validate_governance_reconciliation_snapshot(snapshot)


def test_governance_reconciliation_requires_exact_terminal_ledger_states() -> None:
    snapshot = _valid_reconciliation_snapshot()
    snapshot["reservation_states"] = {
        "settled_actual": 269,
        "settled_conservative": 1,
        "released_pre_send": 1,
    }

    with pytest.raises(script.AcceptanceFailure, match="terminal-state counts drift"):
        script.validate_governance_reconciliation_snapshot(snapshot)


@pytest.mark.parametrize("event_type", script.EXPECTED_PROVIDER_ATTEMPT_AUDIT_COUNTS)
def test_governance_reconciliation_requires_each_provider_attempt_audit(
    event_type: str,
) -> None:
    snapshot = _valid_reconciliation_snapshot()
    audit_event_types = dict(snapshot["audit_event_types"])
    audit_event_types[event_type] -= 1
    snapshot["audit_event_types"] = audit_event_types

    with pytest.raises(script.AcceptanceFailure, match=event_type):
        script.validate_governance_reconciliation_snapshot(snapshot)


def test_governance_reconciliation_rejects_missing_or_noninteger_fields() -> None:
    snapshot = _valid_reconciliation_snapshot()
    del snapshot["duplicate_response_questions"]

    with pytest.raises(script.AcceptanceFailure, match="duplicate_response_questions"):
        script.validate_governance_reconciliation_snapshot(snapshot)

    snapshot = _valid_reconciliation_snapshot()
    snapshot["failed_attempt_count"] = True
    with pytest.raises(script.AcceptanceFailure, match="failed_attempt_count"):
        script.validate_governance_reconciliation_snapshot(snapshot)


def test_topology_records_postgres_max_connections_as_an_integer() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.worker_count = 2
    harness.evidence = {}
    harness.wait_service_healthy = lambda *_args, **_kwargs: []
    harness.wait_api_ready = lambda *_args, **_kwargs: {"payload": {"status": "ready"}}
    harness.initialize_demo = lambda: {"benchmark": {}, "model": {}}
    harness.apply_capacity_policy = lambda: {}
    harness.create_mock_capacity_model = lambda _role: {
        "id": "model-low",
        "provider_type": "mock",
        "enabled": True,
        "api_key_env": None,
    }
    harness.psql = lambda sql: SimpleNamespace(
        stdout="100\n" if "max_connections" in sql else "16.10\n"
    )
    harness.redis_cli = lambda *_args: SimpleNamespace(stdout="redis_version:7.4.0\n")
    harness.runtime_settings = lambda: {}
    harness.host_environment = lambda: {"host": {}, "docker": {}}
    harness.container_resources = lambda _service: []
    harness.write_evidence = lambda: None

    harness.topology_and_data()

    assert harness.evidence["environment"]["postgres_max_connections"] == 100
    assert isinstance(harness.evidence["environment"]["postgres_max_connections"], int)


def test_fairness_summary_proves_low_volume_slice_before_high_backlog_drains() -> None:
    high_run_ids = ["high-a", "high-b"]
    low_run_id = "low"
    audit_events = {
        "high-a": [
            {
                "id": "event-1",
                "event_type": "run_claimed",
                "occurred_at": "2026-08-28T00:00:01Z",
            },
            {
                "id": "event-5",
                "event_type": "run_terminal",
                "occurred_at": "2026-08-28T00:00:05Z",
            },
        ],
        "high-b": [
            {
                "id": "event-2",
                "event_type": "run_claimed",
                "occurred_at": "2026-08-28T00:00:02Z",
            },
            {
                "id": "event-6",
                "event_type": "run_terminal",
                "occurred_at": "2026-08-28T00:00:06Z",
            },
        ],
        low_run_id: [
            {
                "id": "event-3",
                "event_type": "run_claimed",
                "occurred_at": "2026-08-28T00:00:03Z",
            },
            {
                "id": "event-4",
                "event_type": "run_yielded",
                "occurred_at": "2026-08-28T00:00:04Z",
            },
        ],
    }
    observation = {
        "low_run": {"id": low_run_id, "dispatch_count": 1, "completed_questions": 1},
        "high_runs": [
            {"id": "high-a", "status": "pending", "completed_questions": 5},
            {"id": "high-b", "status": "pending", "completed_questions": 5},
        ],
    }

    summary = script.fairness_ordering_summary(
        high_run_ids=high_run_ids,
        low_run_id=low_run_id,
        audit_events=audit_events,
        observation=observation,
    )

    assert summary["low_volume_claim_observed"] is True
    assert summary["low_volume_slice_observed"] is True
    assert summary["high_volume_incomplete_at_low_slice"] == 2
    assert summary["low_claim_before_high_backlog_drained"] is True
    assert [item["role"] for item in summary["ordered_events"][:3]] == [
        "high_volume",
        "high_volume",
        "low_volume",
    ]


def test_capacity_harness_is_mock_only_sanitized_and_make_addressable() -> None:
    capacity_source = _CAPACITY_SCRIPT.read_text(encoding="utf-8")
    acceptance_source = _ACCEPTANCE_SCRIPT.read_text(encoding="utf-8")
    makefile = (_REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")

    assert '"provider_type": "mock"' in acceptance_source
    assert "OPENAI_API_KEY" in acceptance_source
    assert "LLMBENCHLAB_REAL_API_KEY" in capacity_source
    assert "real-Provider capacity" in capacity_source
    assert "llmbenchlab-phase2-capacity-evidence-v1" in capacity_source
    assert "p50" in capacity_source and "p95" in capacity_source and "p99" in capacity_source
    assert "phase2-capacity:" in makefile
    assert "python3 scripts/phase2_capacity.py" in makefile

    sanitized = script.sanitize(
        {
            "database_url": "postgresql://user:password@postgres/db",
            "authorization": "Bearer sk-capacity-secret-marker",
            "safe": "Mock-only",
        }
    )
    assert sanitized == {
        "database_url": "<redacted>",
        "authorization": "<redacted>",
        "safe": "Mock-only",
    }


def test_acceptance_requires_populated_0004_refusal_and_empty_round_trip() -> None:
    source = _ACCEPTANCE_SCRIPT.read_text(encoding="utf-8")

    assert "postgres_populated_0004_downgrade_refusal_and_empty_round_trip" in source
    assert "Cannot downgrade governance schema" in source
    assert "p2roundtrip_" in source
    assert "DATABASE_URL=" in source and "empty_database_url" in source
    assert 'DROP DATABASE IF EXISTS "{}" WITH (FORCE)' in source


def test_acceptance_command_failure_redacts_database_credentials() -> None:
    harness = object.__new__(script.Phase2Acceptance)
    harness.root = _REPOSITORY_ROOT
    harness.env = {}
    harness.evidence = {"commands": []}
    marker = "capacity-password-must-not-leak"

    with pytest.raises(script.AcceptanceFailure) as caught:
        harness.run_command(
            ["false", f"postgresql://capacity:{marker}@postgres/db"],
            timeout=5,
        )

    assert marker not in str(caught.value)
    assert marker not in str(harness.evidence)
    assert "postgresql://<redacted>@postgres/db" in str(caught.value)
