"""Offline unit checks for the dependency-free Phase 2 acceptance harness."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_ACCEPTANCE_SCRIPT = _REPOSITORY_ROOT / "scripts" / "phase2_acceptance.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "phase2_acceptance_script",
        _ACCEPTANCE_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


def _bare_harness() -> object:
    harness = object.__new__(script.Phase2Acceptance)
    harness.require = script.Phase2Acceptance.require.__get__(harness)
    return harness


def test_database_seam_helper_is_syntax_valid_and_names_exact_boundaries() -> None:
    compile(script.DB_SEAM_HELPER_SOURCE, "<phase2-db-seam-helper>", "exec")

    assert '"reserved", "send_started", "response_committed"' in (script.DB_SEAM_HELPER_SOURCE)
    assert "ProviderAttemptDisposition.SETTLED_ACTUAL" in script.DB_SEAM_HELPER_SOURCE
    assert "ResponseDisposition.INSERTED" in script.DB_SEAM_HELPER_SOURCE
    assert "deterministic_database_seam_injection" in script.DB_SEAM_HELPER_SOURCE
    assert '"sigkill_used": False' in script.DB_SEAM_HELPER_SOURCE


def test_database_seam_helper_executes_inside_api_and_returns_allowlisted_evidence() -> None:
    harness = _bare_harness()
    captured: dict[str, object] = {}
    expected = {
        "fault_method": "deterministic_database_seam_injection",
        "sigkill_used": False,
        "mode": "response_committed",
        "run_id": "run-safe",
        "lease_owner": "acceptance-db-seam:response_committed",
        "lease_token": 1,
        "question_id": "question-safe",
        "reservation_id": "reservation-safe",
        "execution_generation": 0,
        "provider_attempt": 1,
        "response_id": "response-safe",
    }

    def fake_compose(*arguments: str, **options: object) -> subprocess.CompletedProcess[str]:
        captured["arguments"] = arguments
        captured["options"] = options
        return subprocess.CompletedProcess(
            list(arguments),
            0,
            stdout=json.dumps(expected) + "\n",
            stderr="",
        )

    harness.compose = fake_compose

    result = harness.run_database_seam_helper(
        "response_committed",
        "run-safe",
        baseline_run_id="baseline-safe",
    )

    arguments = captured["arguments"]
    assert isinstance(arguments, tuple)
    assert arguments[:5] == ("exec", "-T", "api", "python", "-c")
    assert arguments[5] == script.DB_SEAM_HELPER_SOURCE
    assert arguments[6:] == ("response_committed", "run-safe", "baseline-safe")
    assert captured["options"] == {"timeout": 60, "check": False, "record": False}
    assert result == {
        **expected,
        "helper_source_sha256": script.hashlib.sha256(
            script.DB_SEAM_HELPER_SOURCE.encode("utf-8")
        ).hexdigest(),
    }


@pytest.mark.parametrize(
    ("mode", "run_id", "baseline", "message"),
    [
        ("unknown", "run-safe", None, "unsupported database seam mode"),
        ("reserved", "unsafe/run", None, "unsafe Run ID"),
        (
            "response_committed",
            "run-safe",
            None,
            "response commit seam requires a baseline Run",
        ),
    ],
)
def test_database_seam_helper_rejects_unsafe_or_incomplete_requests(
    mode: str,
    run_id: str,
    baseline: str | None,
    message: str,
) -> None:
    harness = _bare_harness()
    harness.compose = lambda *args, **kwargs: pytest.fail(
        "invalid seam input reached Docker Compose"
    )

    with pytest.raises(script.AcceptanceFailure, match=message):
        harness.run_database_seam_helper(
            mode,
            run_id,
            baseline_run_id=baseline,
        )


def test_database_crash_seam_snapshot_hashes_only_allowlisted_projection() -> None:
    harness = _bare_harness()
    captured: dict[str, str] = {}
    projection = {
        "run": {"id": "run-safe", "status": "completed"},
        "question_executions": [],
        "reservations": [],
        "response_count": 15,
        "distinct_response_questions": 15,
        "response_ids": ["response-safe"],
        "responses": [
            {
                "id": "response-safe",
                "question_id": "question-safe",
                "persisted_event_count": 1,
            }
        ],
        "audit_event_type_counts": {"question_evidence_persisted": 15},
    }

    def fake_psql(sql: str) -> subprocess.CompletedProcess[str]:
        captured["sql"] = sql
        return subprocess.CompletedProcess(
            ["psql"],
            0,
            stdout=json.dumps(projection) + "\n",
            stderr="",
        )

    harness.psql = fake_psql

    result = harness.db_crash_seam_snapshot("run-safe")

    assert "raw_response" not in captured["sql"]
    assert "payload->>'disposition'" in captured["sql"]
    assert result == {**projection, "sha256": script.canonical_hash(projection)}


def test_run_all_includes_database_seams_before_outage_and_migration() -> None:
    harness = object.__new__(script.Phase2Acceptance)
    harness.evidence = {"status": "initializing"}
    calls: list[str] = []
    scenario_methods = (
        "setup_stack",
        "topology_scenario",
        "baseline_scenario",
        "api_restart_scenario",
        "worker_crash_scenario",
        "database_crash_seams_scenario",
        "redis_outage_scenario",
        "pending_cancel_scenario",
        "running_cancel_scenario",
        "migration_round_trip_scenario",
    )
    for name in scenario_methods:
        setattr(harness, name, lambda name=name: calls.append(name))
    harness.write_evidence = lambda: calls.append("write_evidence")
    harness.wait_queue_drained = lambda timeout: {"timeout": timeout}
    harness.wait_service_healthy = lambda service, count, timeout: {
        "service": service,
        "count": count,
        "timeout": timeout,
    }
    harness.wait_api_ready = lambda expected_status, timeout: {
        "payload": {"status": expected_status, "timeout": timeout}
    }

    harness.run_all()

    assert calls == ["write_evidence", *scenario_methods]
    assert calls.index("database_crash_seams_scenario") < calls.index("redis_outage_scenario")
    assert calls.index("database_crash_seams_scenario") < calls.index(
        "migration_round_trip_scenario"
    )
    assert harness.evidence["status"] == "running"
    assert harness.evidence["final_invariants"]["queue"] == {"timeout": 60}
