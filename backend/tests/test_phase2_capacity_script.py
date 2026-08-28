"""Offline unit checks for the dependency-free Phase 2 capacity harness."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

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
