"""Fully offline vertical-slice smoke and Runner fault-isolation tests."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select

import app.runners.evaluation_runner as evaluation_runner_module
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import Benchmark, Question
from app.runners.evaluation_runner import EvaluationRunner, _QuestionSnapshot
from app.workers import WorkerService


def _register_mock(
    client, name: str = "Offline Mock", default_parameters: dict | None = None
) -> dict:
    response = client.post(
        "/api/v1/models",
        json={
            "name": name,
            "provider_type": "mock",
            "enabled": True,
            "default_parameters": default_parameters or {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _reload_demo(client) -> dict:
    response = client.post("/api/v1/benchmarks/reload-demo")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["question_count"] == 15
    assert payload["is_demo"] is True
    return payload


def _run_to_terminal(client, model_id: str, benchmark_id: str) -> dict:
    created = client.post(
        "/api/v1/runs",
        json={
            "model_id": model_id,
            "benchmark_id": benchmark_id,
            "temperature": 0,
            "top_p": 1,
            "max_tokens": 64,
            "seed": 42,
            "concurrency": 2,
        },
    )
    assert created.status_code == 202, created.text
    return _wait_for_terminal(client, created.json()["id"])


def _wait_for_terminal(client, run_id: str) -> dict:
    initial = client.get(f"/api/v1/runs/{run_id}")
    assert initial.status_code == 200
    initial_payload = initial.json()
    assert initial_payload["status"] == "pending"
    assert initial_payload["attempt_count"] == 0
    assert initial_payload["lease_owner"] is None
    assert client.get(f"/api/v1/runs/{run_id}/responses").json()["total"] == 0
    assert not hasattr(client.app.state, "task_manager")
    worker = WorkerService(
        SessionLocal,
        get_settings(),
        run_queue=None,
        worker_id=f"test-worker:{run_id}",
    )
    assert asyncio.run(worker.run_once()) is True

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = client.get(f"/api/v1/runs/{run_id}")
        assert current.status_code == 200
        payload = current.json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    pytest.fail("Mock evaluation did not reach a terminal state")


def test_multiple_choice_prompt_uses_stable_key_order() -> None:
    question = _QuestionSnapshot(
        id="question-id",
        external_id="choice-order",
        question_type="multiple_choice",
        prompt="Choose one.",
        choices={"C": "third", "A": "first", "B": "second"},
        reference_answer="A",
        evaluator_config={},
        metadata={},
    )

    messages = EvaluationRunner._render_messages(
        question,
        {"system": "Answer briefly.", "user": "{prompt}\n{choices}"},
    )

    assert messages[1]["content"] == "Choose one.\nA. first\nB. second\nC. third"


def test_blank_system_prompt_is_omitted_for_provider_compatibility() -> None:
    question = _QuestionSnapshot(
        id="question-id",
        external_id="blank-system",
        question_type="multiple_choice",
        prompt="Choose one.",
        choices={"A": "first", "B": "second"},
        reference_answer="A",
        evaluator_config={},
        metadata={},
    )

    messages = EvaluationRunner._render_messages(
        question,
        {"system": "  ", "user": "{prompt}\n{choices}"},
    )

    assert messages == [{"role": "user", "content": "Choose one.\nA. first\nB. second"}]


def test_validated_model_defaults_apply_when_run_fields_are_omitted(client) -> None:
    defaults = {"temperature": 0.4, "top_p": 0.8, "max_tokens": 32, "seed": 7}
    model = _register_mock(client, "Defaulted Mock", defaults)
    benchmark = _reload_demo(client)

    created = client.post(
        "/api/v1/runs",
        json={"model_id": model["id"], "benchmark_id": benchmark["id"]},
    )
    assert created.status_code == 202, created.text
    run = _wait_for_terminal(client, created.json()["id"])

    assert run["status"] == "completed"
    assert run["model_parameters_snapshot"]["generation"] == defaults


def test_parse_error_evidence_identifies_nonempty_truncated_output() -> None:
    assert EvaluationRunner._parse_error_evidence(
        "choice_not_found", {"finish_reason": "length"}
    ) == (
        "output_truncated",
        "Provider stopped at the output token limit before a valid final answer was parsed "
        "(choice_not_found).",
    )
    assert EvaluationRunner._parse_error_evidence(
        "choice_not_found", {"finish_reason": "stop"}
    ) == ("parse_error", "choice_not_found")
    assert EvaluationRunner._parse_error_evidence(None, {"finish_reason": "length"}) == (
        None,
        None,
    )


@pytest.mark.smoke
def test_offline_mock_vertical_slice(client, db_session) -> None:
    model = _register_mock(client)
    benchmark = _reload_demo(client)

    # Reloading the built-in bytes must restore the non-negotiable Demo marker
    # even if identical content was previously imported without that marker.
    benchmark_row = db_session.get(Benchmark, benchmark["id"])
    assert benchmark_row is not None
    benchmark_row.is_demo = False
    db_session.commit()
    benchmark = _reload_demo(client)

    run = _run_to_terminal(client, model["id"], benchmark["id"])

    assert run["status"] == "completed"
    assert run["completed_questions"] == 15
    assert run["correct_questions"] == 15
    assert run["score"] == 100
    assert run["completion_rate"] == 100
    assert run["answered_accuracy"] == 100
    assert run["benchmark_hash_snapshot"] == benchmark["dataset_hash"]
    snapshot = run["model_parameters_snapshot"]
    assert snapshot["model"]["adapter_type"] == "mock"
    assert snapshot["model"]["name"] == "Offline Mock"
    assert float(snapshot["model"]["input_price_per_million"]) == 0
    assert snapshot["benchmark"]["dataset_hash"] == benchmark["dataset_hash"]
    assert snapshot["benchmark"]["question_count"] == 15
    assert snapshot["benchmark"]["is_demo"] is True
    assert snapshot["benchmark"]["schema_version"] == benchmark["schema_version"]
    assert snapshot["benchmark"]["source"] == benchmark["source"]
    assert snapshot["benchmark"]["license"] == benchmark["license"]
    assert snapshot["benchmark"]["dimension"] == benchmark["dimension"]
    assert snapshot["benchmark"]["language"] == benchmark["language"]
    assert snapshot["execution"]["concurrency"] == 2
    assert snapshot["execution"]["timeouts_seconds"] == {
        "connect": 5.0,
        "read": 60.0,
        "write": 30.0,
        "pool": 5.0,
    }
    assert snapshot["execution"]["retry_policy"]["max_attempts"] == 3
    assert "_concurrency" not in snapshot["generation"]

    responses = client.get(f"/api/v1/runs/{run['id']}/responses?limit=100")
    assert responses.status_code == 200
    evidence = responses.json()
    assert evidence["total"] == 15
    assert len(evidence["items"]) == 15
    assert {item["question_type"] for item in evidence["items"]} == {
        "exact_match",
        "multiple_choice",
        "numeric",
    }

    leaderboard = client.get(f"/api/v1/leaderboard?benchmark_id={benchmark['id']}")
    assert leaderboard.status_code == 200
    assert leaderboard.json()["items"][0]["run_id"] == run["id"]
    assert leaderboard.json()["items"][0]["is_demo"] is True

    renamed = client.patch(
        f"/api/v1/models/{model['id']}",
        json={"name": "Renamed Later", "input_price_per_million": 99},
    )
    assert renamed.status_code == 200
    historical = client.get(f"/api/v1/leaderboard?benchmark_id={benchmark['id']}").json()
    assert historical["items"][0]["model_name"] == "Offline Mock"
    frozen_model, *_ = EvaluationRunner(SessionLocal)._load_snapshots(run["id"])
    assert frozen_model.input_price == 0

    summary = client.get("/api/v1/metrics/summary")
    assert summary.status_code == 200
    assert summary.json()["completed_run_count"] == 1
    assert summary.json()["recent_runs"][0]["model_name"] == "Offline Mock"


def test_single_question_error_does_not_fail_run(client, db_session) -> None:
    model = _register_mock(client, "Fault Injection Mock")
    benchmark = _reload_demo(client)
    question = db_session.scalar(
        select(Question).where(Question.benchmark_id == benchmark["id"]).order_by(Question.position)
    )
    question.metadata_ = dict(question.metadata_) | {
        "mock_error": {"error_type": "injected_failure", "message": "offline fault"}
    }
    db_session.commit()

    run = _run_to_terminal(client, model["id"], benchmark["id"])
    assert run["status"] == "completed"
    assert run["completed_questions"] == 15
    assert run["correct_questions"] == 14
    assert run["error_questions"] == 1
    assert run["score"] == pytest.approx(14 / 15 * 100)
    assert run["completion_rate"] == pytest.approx(14 / 15 * 100)
    assert run["answered_accuracy"] == 100
    assert run["input_tokens"] is None
    assert run["output_tokens"] is None
    assert run["estimated_cost"] is None

    responses = client.get(f"/api/v1/runs/{run['id']}/responses?limit=100").json()["items"]
    failed = [item for item in responses if item["error_type"] == "injected_failure"]
    assert len(failed) == 1
    assert failed[0]["score"] == 0

    summary = client.get("/api/v1/metrics/summary")
    assert summary.status_code == 200
    assert summary.json()["total_input_tokens"] is None
    assert summary.json()["total_output_tokens"] is None
    assert summary.json()["total_estimated_cost"] is None


def test_evaluator_error_keeps_generated_evidence(client, monkeypatch) -> None:
    model = _register_mock(client, "Evaluator Fault Mock")
    benchmark = _reload_demo(client)
    original_get_evaluator = evaluation_runner_module.get_evaluator

    class ExplodingEvaluator:
        evaluator_name = "exploding_evaluator_v1"

        def evaluate(self, *_args, **_kwargs):
            raise RuntimeError("offline evaluator fault")

    def get_evaluator_with_fault(question_type: str):
        if question_type == "exact_match":
            return ExplodingEvaluator()
        return original_get_evaluator(question_type)

    monkeypatch.setattr(
        evaluation_runner_module,
        "get_evaluator",
        get_evaluator_with_fault,
    )

    run = _run_to_terminal(client, model["id"], benchmark["id"])
    assert run["status"] == "completed"
    assert run["completed_questions"] == 15
    assert run["correct_questions"] == 10
    assert run["error_questions"] == 5
    assert run["score"] == pytest.approx(10 / 15 * 100)
    assert run["completion_rate"] == 100
    assert run["answered_accuracy"] == 100
    assert run["input_tokens"] == 120
    assert run["output_tokens"] == 30
    assert run["estimated_cost"] == 0

    responses = client.get(f"/api/v1/runs/{run['id']}/responses?limit=100").json()["items"]
    evaluator_errors = [
        item for item in responses if item["error_type"] == "evaluator_internal_error"
    ]
    assert len(evaluator_errors) == 5
    assert all(item["raw_response"] for item in evaluator_errors)
    assert all(item["latency_ms"] == 1 for item in evaluator_errors)
    assert all(item["input_tokens"] == 8 for item in evaluator_errors)
    assert all(item["output_tokens"] == 2 for item in evaluator_errors)
