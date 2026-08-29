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


def _formal_profile_argv() -> list[str]:
    return [
        "--qualification-profile",
        script.FORMAL_QUALIFICATION_PROFILE,
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
    ]


def _formal_worker_state(project: str) -> list[dict[str, object]]:
    return [
        {
            "id": "a" * 64,
            "hostname": "worker-a",
            "project": project,
            "service": "worker",
            "status": "running",
            "health": "healthy",
        },
        {
            "id": "b" * 64,
            "hostname": "worker-b",
            "project": project,
            "service": "worker",
            "status": "running",
            "health": "healthy",
        },
    ]


def _worker_owner(hostname: str, suffix: int) -> str:
    return f"worker:{hostname}:1:00000000-0000-4000-8000-{suffix:012d}"


def _valid_reconciliation_snapshot(
    *,
    runs_per_phase: int = 4,
    backlog_limit: int = 4,
    formal_slo_v2: bool = False,
) -> dict[str, object]:
    expected_counts, expected_states, expected_audit_counts = script.reconciliation_expectations(
        runs_per_phase=runs_per_phase,
        backlog_limit=backlog_limit,
        formal_slo_v2=formal_slo_v2,
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


def test_image_content_sha_ignores_only_dynamic_compose_labels() -> None:
    inspected = {
        "Architecture": "arm64",
        "Os": "linux",
        "Variant": None,
        "RootFS": {"Layers": [f"sha256:{'a' * 64}", f"sha256:{'b' * 64}"]},
        "Config": {
            "Cmd": ["python", "-m", "app"],
            "Env": ["PYTHONUNBUFFERED=1"],
            "Labels": {
                "com.docker.compose.project": "project-a",
                "com.docker.compose.service": "api",
                "com.docker.compose.version": "5.3.1",
                "org.opencontainers.image.revision": "fixed-revision",
            },
        },
    }
    other_project = json.loads(json.dumps(inspected))
    other_project["Config"]["Labels"]["com.docker.compose.project"] = "project-b"
    other_project["Config"]["Labels"]["com.docker.compose.service"] = "worker"

    assert script.image_content_sha256(inspected) == script.image_content_sha256(other_project)

    changed_layer = json.loads(json.dumps(inspected))
    changed_layer["RootFS"]["Layers"][-1] = f"sha256:{'c' * 64}"
    assert script.image_content_sha256(inspected) != script.image_content_sha256(changed_layer)

    changed_config = json.loads(json.dumps(inspected))
    changed_config["Config"]["Cmd"] = ["python", "-m", "different"]
    assert script.image_content_sha256(inspected) != script.image_content_sha256(changed_config)

    changed_vendor_label = json.loads(json.dumps(inspected))
    changed_vendor_label["Config"]["Labels"]["org.opencontainers.image.revision"] = "changed"
    assert script.image_content_sha256(inspected) != script.image_content_sha256(
        changed_vendor_label
    )

    changed_compose_version = json.loads(json.dumps(inspected))
    changed_compose_version["Config"]["Labels"]["com.docker.compose.version"] = "5.4.0"
    assert script.image_content_sha256(inspected) != script.image_content_sha256(
        changed_compose_version
    )


def test_image_content_sha_preserves_none_vs_empty_labels_and_rejects_invalid_labels() -> None:
    base = {
        "Architecture": "arm64",
        "Os": "linux",
        "RootFS": {"Layers": [f"sha256:{'a' * 64}"]},
        "Config": {"Labels": None},
    }
    empty_labels = json.loads(json.dumps(base))
    empty_labels["Config"]["Labels"] = {}
    invalid_labels = json.loads(json.dumps(base))
    invalid_labels["Config"]["Labels"] = []

    assert script.image_content_sha256(base) != script.image_content_sha256(empty_labels)
    with pytest.raises(script.AcceptanceFailure, match=r"invalid Config\.Labels"):
        script.image_content_sha256(invalid_labels)


@pytest.mark.parametrize(
    "layer",
    [
        "sha256:short",
        f"sha256:{'A' * 64}",
        f"sha256:{'g' * 64}",
        f"md5:{'a' * 64}",
    ],
)
def test_image_content_sha_rejects_malformed_layer_identity(layer: str) -> None:
    inspected = {
        "Architecture": "arm64",
        "Os": "linux",
        "RootFS": {"Layers": [layer]},
        "Config": {"Labels": {}},
    }

    with pytest.raises(script.AcceptanceFailure, match="invalid RootFS layers"):
        script.image_content_sha256(inspected)


def test_container_resources_records_raw_and_content_image_identities() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.project = "llmbenchlab-p2-123456789abc"
    harness.require = script.Phase2Acceptance.require.__get__(harness)
    harness.service_container_ids = lambda _service: ["container-id"]
    raw_image_id = f"sha256:{'a' * 64}"
    container_inspect = {
        "Image": raw_image_id,
        "Config": {
            "Labels": {"com.docker.compose.project": harness.project},
        },
        "HostConfig": {
            "Memory": 0,
            "MemorySwap": 0,
            "NanoCpus": 0,
            "CpuQuota": 0,
            "CpuPeriod": 0,
            "PidsLimit": None,
        },
    }
    image_inspect = {
        "Architecture": "arm64",
        "Os": "linux",
        "RootFS": {"Layers": [f"sha256:{'b' * 64}"]},
        "Config": {
            "Cmd": ["python", "-m", "app"],
            "Labels": {"com.docker.compose.project": harness.project},
        },
    }
    commands: list[list[str]] = []

    def run_command(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        payload = container_inspect if command[1] == "inspect" else image_inspect
        return SimpleNamespace(stdout=json.dumps([payload]))

    harness.run_command = run_command

    resources = harness.container_resources("api")

    assert commands == [
        ["docker", "inspect", "container-id"],
        ["docker", "image", "inspect", raw_image_id],
    ]
    assert resources[0]["image_id"] == raw_image_id
    assert resources[0]["image_content_sha256"] == script.image_content_sha256(image_inspect)


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
    assert args.qualification_profile == script.DEFAULT_QUALIFICATION_PROFILE

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


def test_formal_profile_is_one_fixed_switch_with_an_independent_schema() -> None:
    args = script.parse_arguments(_formal_profile_argv())
    harness = script.Phase2Capacity(_REPOSITORY_ROOT, args)
    try:
        assert harness.formal_slo_v2 is True
        assert harness.env["LLMBENCHLAB_COMPOSE_WORKER_EXPECTED_PROCESSES"] == "2"
        assert harness.evidence["schema_version"] == script.CAPACITY_EVIDENCE_SCHEMA_V2
        assert (
            harness.evidence["configuration"]["qualification_profile"]
            == script.FORMAL_QUALIFICATION_PROFILE
        )
        assert {
            field: harness.evidence["configuration"][field]
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
    finally:
        harness._credential_secret_dir.cleanup()

    with pytest.raises(ValueError, match='"differing_fields"'):
        script.parse_arguments(["--qualification-profile", script.FORMAL_QUALIFICATION_PROFILE])


@pytest.mark.parametrize(
    ("argument", "drifted_value", "field"),
    [
        ("--workers", "3", "workers"),
        ("--runs-per-phase", "3", "runs_per_phase"),
        ("--backlog-limit", "5", "backlog_limit"),
        ("--burst-runs", "7", "burst_runs"),
        ("--submit-concurrency", "5", "submit_concurrency"),
        ("--run-concurrency", "2", "run_concurrency"),
        ("--question-quantum", "4", "question_quantum"),
        ("--mock-delay-seconds", "0.09", "mock_delay_seconds"),
        ("--timeout-seconds", "181", "timeout_seconds"),
        ("--lease-seconds", "31", "lease_seconds"),
        ("--heartbeat-seconds", "9", "heartbeat_seconds"),
        ("--worker-poll-seconds", "1.1", "worker_poll_seconds"),
        ("--worker-max-attempts", "4", "worker_max_attempts"),
        ("--retry-backoff-base-seconds", "2", "retry_backoff_base_seconds"),
        ("--retry-backoff-cap-seconds", "31", "retry_backoff_cap_seconds"),
        ("--worker-shutdown-grace-seconds", "31", "worker_shutdown_grace_seconds"),
        ("--redis-block-milliseconds", "1001", "redis_block_milliseconds"),
        ("--redis-operation-timeout-seconds", "1.1", "redis_operation_timeout_seconds"),
    ],
)
def test_formal_profile_rejects_every_fixed_argument_drift(
    argument: str,
    drifted_value: str,
    field: str,
) -> None:
    argv = _formal_profile_argv()
    argv[argv.index(argument) + 1] = drifted_value

    with pytest.raises(ValueError) as exc_info:
        script.parse_arguments(argv)

    assert str(exc_info.value) == (
        f'P2-local-control-plane-v2 fixed configuration drift: {{"differing_fields":["{field}"]}}'
    )


def test_default_profile_retains_v1_schema_and_single_burst_identity() -> None:
    harness = script.Phase2Capacity(_REPOSITORY_ROOT, script.parse_arguments([]))
    try:
        assert harness.formal_slo_v2 is False
        assert harness.env["LLMBENCHLAB_COMPOSE_WORKER_EXPECTED_PROCESSES"] == "2"
        assert harness.evidence["schema_version"] == script.CAPACITY_EVIDENCE_SCHEMA_V1
        assert (
            harness.evidence["configuration"]["qualification_profile"]
            == script.DEFAULT_QUALIFICATION_PROFILE
        )
    finally:
        harness._credential_secret_dir.cleanup()

    configurable = script.parse_arguments(
        [
            "--workers",
            "3",
            "--runs-per-phase",
            "3",
            "--backlog-limit",
            "5",
            "--burst-runs",
            "7",
            "--submit-concurrency",
            "5",
            "--run-concurrency",
            "2",
            "--question-quantum",
            "4",
            "--mock-delay-seconds",
            "0.09",
            "--timeout-seconds",
            "181",
            "--lease-seconds",
            "32",
            "--heartbeat-seconds",
            "10",
            "--worker-poll-seconds",
            "1.1",
            "--worker-max-attempts",
            "4",
            "--retry-backoff-base-seconds",
            "2",
            "--retry-backoff-cap-seconds",
            "31",
            "--worker-shutdown-grace-seconds",
            "31",
            "--redis-block-milliseconds",
            "1001",
            "--redis-operation-timeout-seconds",
            "1.1",
        ]
    )
    assert configurable.qualification_profile == script.DEFAULT_QUALIFICATION_PROFILE
    assert configurable.workers == 3
    assert configurable.run_concurrency == 2
    assert configurable.lease_seconds == 32
    assert configurable.redis_operation_timeout_seconds == 1.1
    configurable_harness = script.Phase2Capacity(_REPOSITORY_ROOT, configurable)
    try:
        assert configurable_harness.env["LLMBENCHLAB_COMPOSE_WORKER_EXPECTED_PROCESSES"] == "3"
    finally:
        configurable_harness._credential_secret_dir.cleanup()


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


def test_formal_burst_claims_map_strict_owners_to_two_validated_hostnames() -> None:
    project = "llmbenchlab-p2-123456789abc"
    owner_a = _worker_owner("worker-a", 1)
    owner_b = _worker_owner("worker-b", 2)
    audit_events = {
        "run-a": [
            {
                "event_type": "run_claimed",
                "worker_id": "legacy-invalid-owner",
                "occurred_at": "2026-08-28T00:00:00Z",
            },
            {
                "event_type": "run_claimed",
                "worker_id": owner_a,
                "occurred_at": "2026-08-28T00:00:02Z",
            },
        ],
        "run-b": [
            {
                "event_type": "run_claimed",
                "worker_id": owner_b,
                "occurred_at": "2026-08-28T00:00:03Z",
            }
        ],
    }

    result = script.burst_worker_participation(
        accepted_run_ids=["run-a", "run-b"],
        audit_events=audit_events,
        worker_state=_formal_worker_state(project),
        project=project,
        backlog_ready_at="2026-08-28T00:00:01Z",
    )

    assert result == {
        "validated_workers": [
            {"container_id": "a" * 64, "hostname": "worker-a"},
            {"container_id": "b" * 64, "hostname": "worker-b"},
        ],
        "claims": [
            {
                "run_id": "run-a",
                "worker_id": owner_a,
                "occurred_at": "2026-08-28T00:00:02Z",
            },
            {
                "run_id": "run-b",
                "worker_id": owner_b,
                "occurred_at": "2026-08-28T00:00:03Z",
            },
        ],
        "distinct_claim_workers": 2,
        "all_claim_workers_validated": True,
    }
    assert "pid" not in result["validated_workers"][0]


def test_formal_worker_state_witness_drops_raw_container_metadata() -> None:
    project = "llmbenchlab-p2-123456789abc"
    raw_workers = _formal_worker_state(project)
    forbidden = {
        "pid": 987654321,
        "name": "raw-worker-name-canary",
        "restart_count": 42424242,
        "exit_code": 31313131,
        "started_at": "raw-started-at-canary",
        "image_id": "sha256:" + "c" * 64,
    }
    for worker in raw_workers:
        worker.update(forbidden)

    witness = script.formal_worker_state_witness(raw_workers)

    assert len(witness) == 2
    assert all(
        set(worker) == {"id", "hostname", "project", "service", "status", "health"}
        for worker in witness
    )
    serialized = json.dumps(witness)
    for key, value in forbidden.items():
        assert f'"{key}"' not in serialized
        assert json.dumps(value) not in serialized


@pytest.mark.parametrize(
    ("owner_a", "owner_b", "message"),
    [
        ("worker-a", _worker_owner("worker-b", 2), "exact runtime format"),
        (
            "worker:worker-a:1:00000000-0000-1000-8000-000000000001",
            _worker_owner("worker-b", 2),
            "not canonical",
        ),
        (_worker_owner("worker-c", 1), _worker_owner("worker-b", 2), "did not map"),
        (_worker_owner("worker-a", 1), _worker_owner("worker-a", 1), "exactly two"),
    ],
)
def test_formal_burst_rejects_legacy_invalid_unmapped_or_single_owner(
    owner_a: str,
    owner_b: str,
    message: str,
) -> None:
    project = "llmbenchlab-p2-123456789abc"
    events = {
        "run-a": [
            {
                "event_type": "run_claimed",
                "worker_id": owner_a,
                "occurred_at": "2026-08-28T00:00:02Z",
            }
        ],
        "run-b": [
            {
                "event_type": "run_claimed",
                "worker_id": owner_b,
                "occurred_at": "2026-08-28T00:00:03Z",
            }
        ],
    }

    with pytest.raises(script.AcceptanceFailure, match=message):
        script.burst_worker_participation(
            accepted_run_ids=["run-a", "run-b"],
            audit_events=events,
            worker_state=_formal_worker_state(project),
            project=project,
            backlog_ready_at="2026-08-28T00:00:01Z",
        )


def test_formal_burst_rejects_a_third_owner_even_on_a_validated_hostname() -> None:
    project = "llmbenchlab-p2-123456789abc"
    events = {
        "run-a": [
            {
                "event_type": "run_claimed",
                "worker_id": _worker_owner("worker-a", 1),
                "occurred_at": "2026-08-28T00:00:02Z",
            },
            {
                "event_type": "run_claimed",
                "worker_id": _worker_owner("worker-a", 3),
                "occurred_at": "2026-08-28T00:00:03Z",
            },
        ],
        "run-b": [
            {
                "event_type": "run_claimed",
                "worker_id": _worker_owner("worker-b", 2),
                "occurred_at": "2026-08-28T00:00:04Z",
            }
        ],
    }

    with pytest.raises(script.AcceptanceFailure, match="exactly two"):
        script.burst_worker_participation(
            accepted_run_ids=["run-a", "run-b"],
            audit_events=events,
            worker_state=_formal_worker_state(project),
            project=project,
            backlog_ready_at="2026-08-28T00:00:01Z",
        )


def test_formal_burst_segmented_timing_keeps_utc_and_monotonic_domains_separate() -> None:
    project = "llmbenchlab-p2-123456789abc"
    audit_events = {
        "run-a": [
            {
                "event_type": "run_claimed",
                "worker_id": _worker_owner("worker-a", 1),
                "occurred_at": "2026-08-28T00:00:02Z",
            },
            {
                "event_type": "run_yielded",
                "worker_id": _worker_owner("worker-a", 1),
                "occurred_at": "2026-08-28T00:00:03Z",
            },
        ],
        "run-b": [
            {
                "event_type": "run_claimed",
                "worker_id": _worker_owner("worker-b", 2),
                "occurred_at": "2026-08-28T00:00:04Z",
            },
            {
                "event_type": "run_yielded",
                "worker_id": _worker_owner("worker-b", 2),
                "occurred_at": "2026-08-28T00:00:05Z",
            },
        ],
    }
    participation = script.burst_worker_participation(
        accepted_run_ids=["run-a", "run-b"],
        audit_events=audit_events,
        worker_state=_formal_worker_state(project),
        project=project,
        backlog_ready_at="2026-08-28T00:00:01Z",
    )

    timing = script.burst_segmented_timing(
        final_runs=[
            {"id": "run-a", "finished_at": "2026-08-28T00:00:08Z"},
            {"id": "run-b", "finished_at": "2026-08-28T00:00:10Z"},
        ],
        audit_events=audit_events,
        participation=participation,
        suspend_completed_at="2026-08-28T00:00:00Z",
        backlog_ready_at="2026-08-28T00:00:01Z",
        restore_completed_at="2026-08-28T00:00:01.500000Z",
        suspend_seconds=0.25,
        backlog_build_seconds=0.5,
        restore_command_seconds=0.125,
        drain_seconds=9.0,
    )

    assert timing["clock_domains"] == {
        "monotonic_seconds": "process_monotonic",
        "durable_utc": "database_utc",
    }
    assert timing["monotonic_seconds"] == {
        "suspend": 0.25,
        "backlog_build": 0.5,
        "restore_command": 0.125,
        "drain": 9.0,
    }
    assert timing["durable_seconds"]["backlog_ready_to_first_claim"] == 1.0
    assert timing["durable_seconds"]["backlog_ready_to_all_workers_first_claim"] == 3.0
    assert timing["durable_seconds"]["adjacent_claim_or_yield_gap"]["samples"] == [
        1.0,
        1.0,
        1.0,
    ]
    assert timing["durable_seconds"]["first_claim_to_finish"]["samples"] == [
        6.0,
        6.0,
    ]


def test_formal_burst_admission_requires_exact_four_202_and_two_typed_429() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.burst_runs = 6
    harness.backlog_limit = 4
    harness.require = script.Phase2Acceptance.require.__get__(harness)
    submissions = {
        "status_counts": {"202": 4, "429": 2},
        "accepted": [{"id": f"run-{index}"} for index in range(4)],
        "rejected": [
            {
                "status_code": 429,
                "payload": {
                    "detail": {
                        "code": "run_backlog_full",
                        "limit": 4,
                    }
                },
            }
            for _index in range(2)
        ],
    }

    assert harness._validate_burst_submissions(submissions) == (
        ["run-0", "run-1", "run-2", "run-3"],
        2,
    )

    submissions["rejected"][1]["payload"]["detail"]["code"] = "different"
    with pytest.raises(script.AcceptanceFailure, match="typed local admission"):
        harness._validate_burst_submissions(submissions)


def test_default_and_formal_burst_wrappers_are_not_combinable() -> None:
    harness = object.__new__(script.Phase2Capacity)
    calls: list[dict[str, object]] = []
    harness._backlog_burst = lambda **kwargs: calls.append(kwargs) or {}

    harness.bounded_queue_burst()
    harness.warmed_pause_burst()
    harness.cold_start_burst()

    assert calls == [
        {
            "name": "bounded_queue_burst_and_drain",
            "barrier": "cold_start",
            "require_worker_participation": False,
        },
        {
            "name": "warmed_pause_burst_and_drain",
            "barrier": "warmed_pause",
            "require_worker_participation": True,
        },
        {
            "name": "cold_start_burst_and_drain",
            "barrier": "cold_start",
            "require_worker_participation": True,
        },
    ]


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
    assert result["throughput"]["questions_per_second"] == round(
        2 / result["wall_duration_seconds"], 6
    )
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


def test_formal_run_all_appends_fixed_warmed_then_cold_bursts() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.evidence = {}
    harness.worker_count = 2
    harness.measurement_order = "multi_then_single"
    harness.formal_slo_v2 = True
    calls: list[str] = []
    harness.write_evidence = lambda: None
    harness.setup_stack = lambda: None
    harness.topology_and_data = lambda: None

    def measurement(name: str, workers: int) -> dict[str, object]:
        calls.append(name)
        return {
            "run_ids": [f"{name}-run"],
            "throughput": {"questions_per_second": float(workers)},
        }

    harness.run_measurement_phase = measurement
    harness.scale_workers = lambda _count: []
    harness.warmed_pause_burst = lambda: (
        calls.append("warmed_pause_burst_and_drain")
        or {"throughput": {"questions_per_second": 7.0}}
    )
    harness.cold_start_burst = lambda: (
        calls.append("cold_start_burst_and_drain") or {"throughput": {"questions_per_second": 6.0}}
    )
    harness.bounded_queue_burst = lambda: pytest.fail("default burst must not run")
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

    assert calls == [
        "configured_multi_worker_baseline",
        "single_worker_reference",
        "warmed_pause_burst_and_drain",
        "cold_start_burst_and_drain",
    ]
    assert harness.evidence["comparison"]["warmed_pause_burst_questions_per_second"] == 7.0
    assert harness.evidence["comparison"]["cold_start_burst_questions_per_second"] == 6.0
    assert "bounded_burst_questions_per_second" not in harness.evidence["comparison"]


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


def test_formal_governance_reconciliation_requires_22_330_331() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.runs_per_phase = 4
    harness.backlog_limit = 4
    harness.formal_slo_v2 = True
    expected = _valid_reconciliation_snapshot(formal_slo_v2=True)
    harness.psql = lambda _query: SimpleNamespace(stdout=json.dumps(expected))

    assert harness.governance_reconciliation() == expected
    assert expected["runs"] == 22
    assert expected["responses"] == 330
    assert expected["question_executions"] == 330
    assert expected["reservations"] == 331
    assert expected["reservation_states"] == {
        "settled_actual": 330,
        "settled_conservative": 1,
    }
    assert expected["audit_event_types"] == {
        "provider_attempt_reserved": 331,
        "provider_attempt_send_started": 331,
        "provider_attempt_settled": 331,
        "question_evidence_persisted": 330,
    }


def _capacity_cleanup_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pre_cleanup_images: set[str],
) -> script.Phase2Capacity:
    harness = object.__new__(script.Phase2Capacity)
    harness.project = "llmbenchlab-p2-123456789abc"
    harness.stack_touched = True
    harness.formal_slo_v2 = True
    harness.evidence = {"cleanup": {}}
    harness._backend_image_ids_before_cleanup = lambda: pre_cleanup_images

    def base_cleanup(self: object) -> None:
        harness.evidence["cleanup"] = {
            "status": "passed",
            "remaining_containers": [],
            "remaining_project_volumes": [],
            "remaining_project_networks": [],
        }
        return None

    monkeypatch.setattr(script.Phase2Acceptance, "cleanup", base_cleanup)
    return harness


def _cleanup_image_payload(
    image_id: str,
    *,
    project: str = "llmbenchlab-p2-123456789abc",
    tags: list[str] | None = None,
) -> dict[str, object]:
    return {
        "Id": image_id,
        "Config": {
            "Env": ["SHOULD-NOT-ENTER-EVIDENCE=marker"],
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "worker",
            },
        },
        "RepoTags": tags if tags is not None else ["llmbenchlab-backend:p2-123456789abc"],
        "RepoDigests": ["llmbenchlab-backend@sha256:" + "d" * 64],
        "RootFS": {"Layers": ["sha256:" + "e" * 64]},
    }


def test_capacity_cleanup_removes_exact_tag_and_retains_only_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "a" * 64
    harness = _capacity_cleanup_harness(monkeypatch, pre_cleanup_images={image_id})
    commands: list[tuple[list[str], dict[str, object]]] = []
    image_list_calls = 0

    def run_command(command: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal image_list_calls
        commands.append((command, kwargs))
        if command[:3] == ["docker", "image", "ls"]:
            image_list_calls += 1
            stdout = f"{image_id}\n" if image_list_calls == 1 else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([_cleanup_image_payload(image_id)]),
                stderr="",
            )
        if command[:3] == ["docker", "container", "ls"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "image", "rm"]:
            assert command == [
                "docker",
                "image",
                "rm",
                "llmbenchlab-backend:p2-123456789abc",
            ]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    harness.run_command = run_command

    assert harness.cleanup() is None
    assert harness.evidence["cleanup"] == {
        "status": "passed",
        "remaining_containers": [],
        "remaining_project_volumes": [],
        "remaining_project_networks": [],
        "image_cleanup_status": "passed",
        "project_image_candidates": 1,
        "removed_project_images": 1,
        "retained_shared_project_images": 0,
        "remaining_project_images": 0,
    }
    for command, kwargs in commands:
        if command[:2] == ["docker", "image"]:
            assert kwargs["check"] is False
            assert kwargs["record"] is False
    serialized = json.dumps(harness.evidence)
    assert image_id not in serialized
    assert "llmbenchlab-backend:p2-123456789abc" not in serialized
    assert "SHOULD-NOT-ENTER-EVIDENCE" not in serialized
    flattened_commands = " ".join(" ".join(command) for command, _kwargs in commands)
    assert "--force" not in flattened_commands
    assert "prune" not in flattened_commands
    assert "--rmi" not in flattened_commands


def test_capacity_cleanup_never_touches_images_when_generic_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "a" * 64
    harness = object.__new__(script.Phase2Capacity)
    harness.project = "llmbenchlab-p2-123456789abc"
    harness.stack_touched = True
    harness.formal_slo_v2 = True
    harness.evidence = {"cleanup": {}}
    harness._backend_image_ids_before_cleanup = lambda: {image_id}

    def failed_base_cleanup(_self: object) -> str:
        harness.evidence["cleanup"] = {
            "status": "failed",
            "remaining_containers": ["container"],
            "remaining_project_volumes": [],
            "remaining_project_networks": [],
        }
        return "isolated Compose project cleanup was incomplete"

    monkeypatch.setattr(script.Phase2Acceptance, "cleanup", failed_base_cleanup)
    harness.run_command = lambda *_args, **_kwargs: pytest.fail("image commands must not run")

    assert harness.cleanup() == "isolated Compose project cleanup was incomplete"
    assert harness.evidence["cleanup"]["image_cleanup_status"] == "not_attempted"


def test_formal_capacity_cleanup_requires_one_candidate_and_one_verified_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _capacity_cleanup_harness(monkeypatch, pre_cleanup_images=set())
    harness.run_command = lambda command, **_kwargs: SimpleNamespace(
        returncode=0,
        stdout="",
        stderr="",
    )

    assert harness.cleanup() == "formal capacity image cleanup counts were not exact"
    assert harness.evidence["cleanup"]["image_cleanup_status"] == "failed"
    assert (
        harness.evidence["cleanup"]["project_image_candidates"],
        harness.evidence["cleanup"]["removed_project_images"],
        harness.evidence["cleanup"]["retained_shared_project_images"],
        harness.evidence["cleanup"]["remaining_project_images"],
    ) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "case",
    ["label_mismatch", "extra_alias", "container_reference", "multiple", "id_mismatch"],
)
def test_capacity_cleanup_fails_closed_for_unsafe_candidates(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    image_a = "sha256:" + "a" * 64
    image_b = "sha256:" + "b" * 64
    candidates = [image_a, image_b] if case == "multiple" else [image_a]
    pre_images = set(candidates)
    if case == "id_mismatch":
        pre_images = {image_b}
    harness = _capacity_cleanup_harness(monkeypatch, pre_cleanup_images=pre_images)
    commands: list[list[str]] = []

    def run_command(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if command[:3] == ["docker", "image", "ls"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(candidates) + "\n", stderr="")
        if command[:3] == ["docker", "image", "inspect"]:
            inspected_id = command[-1]
            project = "llmbenchlab-p2-ffffffffffff" if case == "label_mismatch" else harness.project
            tags = (
                [
                    "llmbenchlab-backend:p2-123456789abc",
                    "llmbenchlab-backend:shared",
                ]
                if case == "extra_alias"
                else None
            )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [_cleanup_image_payload(inspected_id, project=project, tags=tags)]
                ),
                stderr="",
            )
        if command[:3] == ["docker", "container", "ls"]:
            stdout = "c" * 64 + "\n" if case == "container_reference" else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        raise AssertionError(command)

    harness.run_command = run_command

    assert harness.cleanup() == "capacity image cleanup safety validation failed"
    assert harness.evidence["cleanup"]["image_cleanup_status"] == "failed"
    assert harness.evidence["cleanup"]["project_image_candidates"] == len(candidates)
    assert harness.evidence["cleanup"]["removed_project_images"] == 0
    assert harness.evidence["cleanup"]["remaining_project_images"] == len(candidates)
    assert not any(command[:3] == ["docker", "image", "rm"] for command in commands)


@pytest.mark.parametrize("removal_succeeds", [False, True])
def test_capacity_cleanup_fails_when_removal_fails_or_image_remains(
    monkeypatch: pytest.MonkeyPatch,
    removal_succeeds: bool,
) -> None:
    image_id = "sha256:" + "a" * 64
    harness = _capacity_cleanup_harness(monkeypatch, pre_cleanup_images={image_id})

    def run_command(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[:3] == ["docker", "image", "ls"]:
            return SimpleNamespace(returncode=0, stdout=image_id + "\n", stderr="")
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([_cleanup_image_payload(image_id)]),
                stderr="",
            )
        if command[:3] == ["docker", "container", "ls"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "image", "rm"]:
            return SimpleNamespace(
                returncode=0 if removal_succeeds else 1,
                stdout="",
                stderr="fixed failure",
            )
        raise AssertionError(command)

    harness.run_command = run_command

    expected = (
        "capacity image cleanup left a project image"
        if removal_succeeds
        else "capacity image cleanup removal failed"
    )
    assert harness.cleanup() == expected
    assert harness.evidence["cleanup"]["image_cleanup_status"] == "failed"
    assert harness.evidence["cleanup"]["removed_project_images"] == 0
    assert harness.evidence["cleanup"]["remaining_project_images"] == 1


@pytest.mark.parametrize(
    ("timeout_command", "expected_error"),
    [
        ("container_reference", "capacity image cleanup reference verification failed"),
        ("image_removal", "capacity image cleanup removal failed"),
    ],
)
def test_capacity_cleanup_redacts_image_command_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    timeout_command: str,
    expected_error: str,
) -> None:
    image_id = "sha256:" + "a" * 64
    image_tag = "llmbenchlab-backend:p2-123456789abc"
    config_secret = "SHOULD-NOT-ENTER-EVIDENCE=marker"
    harness = _capacity_cleanup_harness(monkeypatch, pre_cleanup_images={image_id})
    image_list_calls = 0

    def run_command(command: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal image_list_calls
        if command[:3] == ["docker", "image", "ls"]:
            image_list_calls += 1
            return SimpleNamespace(returncode=0, stdout=image_id + "\n", stderr="")
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([_cleanup_image_payload(image_id)]),
                stderr="",
            )
        if command[:3] == ["docker", "container", "ls"]:
            if timeout_command == "container_reference":
                raise script.AcceptanceFailure(
                    f"timed out: ancestor={image_id} {image_tag} {config_secret}"
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "image", "rm"]:
            assert timeout_command == "image_removal"
            raise script.AcceptanceFailure(f"timed out: {image_id} {image_tag} {config_secret}")
        raise AssertionError(command)

    harness.run_command = run_command

    cleanup_error = harness.cleanup()

    assert cleanup_error == expected_error
    assert image_list_calls == 2
    assert harness.evidence["cleanup"]["image_cleanup_status"] == "failed"
    assert harness.evidence["cleanup"]["removed_project_images"] == 0
    assert harness.evidence["cleanup"]["remaining_project_images"] == 1
    serialized = json.dumps({"cleanup_error": cleanup_error, "evidence": harness.evidence})
    assert image_id not in serialized
    assert image_tag not in serialized
    assert config_secret not in serialized


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


@pytest.mark.parametrize(
    ("expected", "docker_status"),
    [(True, "paused"), (False, "running")],
)
def test_worker_pause_state_uses_docker_engine_status(
    expected: bool,
    docker_status: str,
) -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.project = "llmbenchlab-p2-123456789abc"
    harness.require = script.Phase2Acceptance.require.__get__(harness)
    workers = _formal_worker_state(harness.project)

    def run_command(command: list[str], **_kwargs: object) -> SimpleNamespace:
        assert command == ["docker", "inspect", "a" * 64, "b" * 64]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "Id": worker["id"],
                        "Config": {
                            "Labels": {
                                "com.docker.compose.project": harness.project,
                                "com.docker.compose.service": "worker",
                            }
                        },
                        "State": {"Status": docker_status, "Paused": expected},
                    }
                    for worker in workers
                ]
            ),
            stderr="",
        )

    harness.run_command = run_command

    harness._assert_worker_pause_state(workers, expected=expected)


def test_warmed_burst_unpauses_full_worker_set_when_second_pause_fails() -> None:
    harness = object.__new__(script.Phase2Capacity)
    harness.project = "llmbenchlab-p2-123456789abc"
    harness.worker_count = 2
    harness.evidence = {"measurements": []}
    workers = _formal_worker_state(harness.project)
    harness.require = script.Phase2Acceptance.require.__get__(harness)
    harness.service_metas = lambda *_args, **_kwargs: workers
    harness.wait_queue_drained = lambda **_kwargs: {}
    harness.task_metrics = lambda: {"managed_backlog": 0}
    commands: list[list[str]] = []

    def run_command(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if command == ["docker", "pause", "b" * 64]:
            raise script.AcceptanceFailure("second pause failed")
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Id": worker["id"],
                            "Config": {
                                "Labels": {
                                    "com.docker.compose.project": harness.project,
                                    "com.docker.compose.service": "worker",
                                }
                            },
                            "State": {"Status": "running", "Paused": False},
                        }
                        for worker in workers
                    ]
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    harness.run_command = run_command

    with pytest.raises(script.AcceptanceFailure, match="second pause failed"):
        harness.warmed_pause_burst()

    assert commands == [
        ["docker", "pause", "a" * 64],
        ["docker", "pause", "b" * 64],
        ["docker", "unpause", "a" * 64],
        ["docker", "unpause", "b" * 64],
        ["docker", "inspect", "a" * 64, "b" * 64],
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

    equal_timestamp_events = {
        run_id: [dict(event) for event in events] for run_id, events in audit_events.items()
    }
    equal_timestamp_events[low_run_id][0]["occurred_at"] = "2026-08-28T00:00:06Z"
    equal_timestamp = script.fairness_ordering_summary(
        high_run_ids=high_run_ids,
        low_run_id=low_run_id,
        audit_events=equal_timestamp_events,
        observation=observation,
    )
    assert equal_timestamp["low_claim_before_high_backlog_drained"] is False


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


def test_acceptance_requires_both_populated_guards_and_empty_round_trips() -> None:
    source = _ACCEPTANCE_SCRIPT.read_text(encoding="utf-8")

    assert "postgres_populated_0005_and_0004_downgrade_guards_with_empty_round_trips" in source
    assert "Cannot downgrade Worker progress schema" in source
    assert "Cannot downgrade governance schema" in source
    assert "worker_processes" in source
    assert 'PRE_GOVERNANCE_REVISION = "20260827_0003"' in source
    assert 'GOVERNANCE_REVISION = "20260827_0004"' in source
    assert 'WORKER_PROGRESS_REVISION = "20260828_0005"' in source
    assert 'DATABASE_HEAD_REVISION = "20260829_0006"' in source
    assert "application_worker_progress_0005_to_0004_guard" in source
    assert "isolated_governance_0004_to_0003_guard" in source
    assert "worker_progress_0005_to_0004_round_trip" in source
    assert "governance_0004_to_0003_round_trip" in source
    assert "p2roundtrip_" in source
    assert "p2governance_" in source
    assert "DATABASE_URL=" in source and "empty_database_url" in source
    assert source.count("DROP DATABASE IF EXISTS") >= 2
    assert source.count("WITH (FORCE)") >= 2


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
