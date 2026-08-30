"""Fixed-block Run progress projection and canonical live evidence metrics."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.logging import normalize_request_path
from app.models import (
    Benchmark,
    EvaluationResponse,
    EvaluationRun,
    Model,
    ProviderType,
    Question,
    QuestionType,
    RunStatus,
)
from app.schemas.evaluation_progress import PROGRESS_BLOCK_SIZE
from app.services.run_progress import progress_block_count


@pytest.mark.parametrize(
    ("total_questions", "expected_blocks"),
    [(0, 0), (1, 1), (12_032, 24), (20_000, 40)],
)
def test_progress_block_count_covers_supported_run_sizes(
    total_questions: int,
    expected_blocks: int,
) -> None:
    assert progress_block_count(total_questions) == expected_blocks


def _progress_fixture(db_session) -> tuple[EvaluationRun, list[Question]]:
    model = Model(id="progress-model", name="Progress Mock", provider_type=ProviderType.MOCK)
    benchmark = Benchmark(
        id="progress-benchmark",
        slug="progress-benchmark",
        name="Progress benchmark",
        version="1.0.0",
        description="fixture",
        dimension="general",
        language="en",
        license="MIT",
        source="local",
        evaluator_type="exact_match",
        evaluator_config={},
        prompt_template={},
        dataset_hash="progress-hash",
        question_count=515,
    )
    questions = [
        Question(
            id=f"progress-question-{position}",
            benchmark_id=benchmark.id,
            external_id=f"progress-q{position + 1}",
            position=position,
            question_type=QuestionType.EXACT_MATCH,
            prompt=f"Question {position + 1}",
            reference_answer="one",
        )
        for position in range(515)
    ]
    run = EvaluationRun(
        id="progress-run",
        model_id=model.id,
        benchmark_id=benchmark.id,
        status=RunStatus.COMPLETED,
        model_parameters_snapshot={},
        benchmark_hash_snapshot=benchmark.dataset_hash,
        prompt_template_snapshot={},
        total_questions=515,
        completed_questions=4,
        correct_questions=0,
        error_questions=0,
        score=0,
        completion_rate=0,
        answered_accuracy=None,
        average_latency_ms=None,
        input_tokens=None,
        output_tokens=None,
        estimated_cost=None,
    )
    created_at = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)
    responses = [
        EvaluationResponse(
            id="progress-response-pass",
            run_id=run.id,
            question_id=questions[511].id,
            raw_response="one",
            parsed_answer="one",
            reference_answer_snapshot="one",
            score=1,
            evaluator_name="exact_match_v1",
            latency_ms=100,
            input_tokens=10,
            output_tokens=5,
            estimated_cost=Decimal("0.001"),
            created_at=created_at,
        ),
        EvaluationResponse(
            id="progress-response-wrong",
            run_id=run.id,
            question_id=questions[0].id,
            raw_response="two",
            parsed_answer="two",
            reference_answer_snapshot="one",
            score=0,
            evaluator_name="exact_match_v1",
            latency_ms=200,
            input_tokens=20,
            output_tokens=10,
            estimated_cost=None,
            created_at=created_at,
        ),
        EvaluationResponse(
            id="progress-response-error",
            run_id=run.id,
            question_id=questions[512].id,
            raw_response=None,
            parsed_answer=None,
            reference_answer_snapshot="one",
            # Deliberately inconsistent evidence proves error_type has outcome priority.
            score=1,
            evaluator_name="exact_match_v1",
            latency_ms=300,
            input_tokens=None,
            output_tokens=None,
            estimated_cost=None,
            error_type="provider_error",
            error_message="sensitive upstream detail omitted from compact progress",
            created_at=created_at,
        ),
        EvaluationResponse(
            id="progress-response-empty",
            run_id=run.id,
            question_id=questions[514].id,
            raw_response="",
            parsed_answer=None,
            reference_answer_snapshot="one",
            score=0,
            evaluator_name="exact_match_v1",
            latency_ms=None,
            input_tokens=0,
            output_tokens=0,
            estimated_cost=Decimal("0"),
            created_at=created_at,
        ),
    ]
    db_session.add_all([model, benchmark, *questions, run, *responses])
    db_session.commit()
    return run, questions


def test_progress_index_returns_one_snapshot_of_canonical_metrics_and_block_counts(
    client,
    db_session,
) -> None:
    run, _ = _progress_fixture(db_session)

    response = client.get(f"/api/v1/runs/{run.id}/progress")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    progress = response.json()
    assert set(progress) == {
        "block_size",
        "total_questions",
        "completed_questions",
        "correct_questions",
        "error_questions",
        "score",
        "completion_rate",
        "answered_accuracy",
        "average_latency_ms",
        "known_input_tokens",
        "known_output_tokens",
        "input_token_reported_responses",
        "output_token_reported_responses",
        "known_estimated_cost",
        "estimated_cost_reported_responses",
        "blocks",
    }
    assert progress["block_size"] == PROGRESS_BLOCK_SIZE
    assert progress["total_questions"] == 515
    assert progress["completed_questions"] == 4
    # Canonical protocol-v1 aggregation rounds score_sum, even for malformed
    # score=1/error evidence; the block outcome still gives error priority.
    assert progress["correct_questions"] == 2
    assert progress["error_questions"] == 1
    assert progress["score"] == pytest.approx(2 / 515 * 100)
    assert progress["completion_rate"] == pytest.approx(2 / 515 * 100)
    assert progress["answered_accuracy"] == 100
    assert progress["average_latency_ms"] == 200
    assert progress["known_input_tokens"] == 30
    assert progress["known_output_tokens"] == 15
    assert progress["input_token_reported_responses"] == 3
    assert progress["output_token_reported_responses"] == 3
    assert progress["known_estimated_cost"] == pytest.approx(0.001)
    assert progress["estimated_cost_reported_responses"] == 2
    assert progress["blocks"] == [
        {"block_index": 0, "response_count": 2},
        {"block_index": 1, "response_count": 2},
    ]

    db_session.expire_all()
    persisted = db_session.get(EvaluationRun, run.id)
    assert persisted is not None
    assert persisted.input_tokens is None
    assert persisted.output_tokens is None
    assert persisted.estimated_cost is None


def test_progress_blocks_return_only_allowlisted_cells_in_absolute_position_order(
    client,
    db_session,
) -> None:
    run, _ = _progress_fixture(db_session)

    first_response = client.get(f"/api/v1/runs/{run.id}/progress/blocks/0")
    second_response = client.get(f"/api/v1/runs/{run.id}/progress/blocks/1")

    assert first_response.status_code == second_response.status_code == 200
    assert first_response.headers["cache-control"] == "no-store"
    assert second_response.headers["cache-control"] == "no-store"
    first = first_response.json()
    second = second_response.json()
    assert [item["position"] for item in first["items"]] == [0, 511]
    assert [item["outcome"] for item in first["items"]] == ["wrong", "passed"]
    assert [item["position"] for item in second["items"]] == [512, 514]
    assert [item["outcome"] for item in second["items"]] == ["error", "wrong"]
    assert second["items"][0]["score"] == 1
    assert second["items"][0]["error_type"] == "provider_error"
    assert second["items"][1]["input_tokens"] == 0
    assert second["items"][1]["output_tokens"] == 0
    assert second["items"][1]["estimated_cost"] == 0

    required_cell_fields = {
        "position",
        "outcome",
        "score",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "estimated_cost",
        "error_type",
    }
    assert all(set(item) == required_cell_fields for item in first["items"] + second["items"])
    forbidden = {
        "id",
        "run_id",
        "question_id",
        "question_external_id",
        "prompt",
        "choices",
        "raw_response",
        "parsed_answer",
        "reference_answer_snapshot",
        "error_message",
        "provider_request_id",
        "returned_model",
        "system_fingerprint",
        "finish_reason",
    }
    assert all(forbidden.isdisjoint(item) for item in first["items"] + second["items"])
    assert "sensitive upstream detail" not in second_response.text


def test_progress_block_can_lead_an_older_index_and_the_next_index_converges(
    client,
    db_session,
) -> None:
    run, questions = _progress_fixture(db_session)
    first_index = client.get(f"/api/v1/runs/{run.id}/progress").json()

    db_session.add(
        EvaluationResponse(
            id="progress-response-concurrent",
            run_id=run.id,
            question_id=questions[513].id,
            raw_response="one",
            parsed_answer="one",
            reference_answer_snapshot="one",
            score=1,
            evaluator_name="exact_match_v1",
            latency_ms=50,
            input_tokens=1,
            output_tokens=1,
            estimated_cost=Decimal("0"),
        )
    )
    db_session.commit()

    newer_block = client.get(f"/api/v1/runs/{run.id}/progress/blocks/1").json()
    converged_index = client.get(f"/api/v1/runs/{run.id}/progress").json()

    assert first_index["blocks"][1]["response_count"] == 2
    assert [item["position"] for item in newer_block["items"]] == [512, 513, 514]
    assert converged_index["blocks"][1]["response_count"] == 3
    assert converged_index["completed_questions"] == 5


def test_progress_index_and_block_represent_an_empty_run_without_inventing_facts(
    client,
    db_session,
) -> None:
    model = Model(id="empty-progress-model", name="Empty Mock", provider_type=ProviderType.MOCK)
    benchmark = Benchmark(
        id="empty-progress-benchmark",
        slug="empty-progress-benchmark",
        name="Empty progress benchmark",
        version="1.0.0",
        description="fixture",
        dimension="general",
        language="en",
        license="MIT",
        source="local",
        evaluator_type="exact_match",
        evaluator_config={},
        prompt_template={},
        dataset_hash="empty-progress-hash",
        question_count=1,
    )
    question = Question(
        id="empty-progress-question",
        benchmark_id=benchmark.id,
        external_id="empty-progress-q1",
        position=0,
        question_type=QuestionType.EXACT_MATCH,
        prompt="Question",
        reference_answer="one",
    )
    run = EvaluationRun(
        id="empty-progress-run",
        model_id=model.id,
        benchmark_id=benchmark.id,
        status=RunStatus.PENDING,
        model_parameters_snapshot={},
        benchmark_hash_snapshot=benchmark.dataset_hash,
        prompt_template_snapshot={},
        total_questions=1,
    )
    db_session.add_all([model, benchmark, question, run])
    db_session.commit()

    index = client.get(f"/api/v1/runs/{run.id}/progress").json()
    block = client.get(f"/api/v1/runs/{run.id}/progress/blocks/0").json()

    assert index["completed_questions"] == 0
    assert index["correct_questions"] == 0
    assert index["error_questions"] == 0
    assert index["score"] == 0
    assert index["completion_rate"] == 0
    assert index["answered_accuracy"] is None
    assert index["average_latency_ms"] is None
    assert index["known_input_tokens"] == 0
    assert index["known_output_tokens"] == 0
    assert index["input_token_reported_responses"] == 0
    assert index["output_token_reported_responses"] == 0
    assert index["known_estimated_cost"] == 0
    assert index["estimated_cost_reported_responses"] == 0
    assert index["blocks"] == [{"block_index": 0, "response_count": 0}]
    assert block == {"block_index": 0, "items": []}


def test_progress_block_bounds_and_missing_run_are_typed(client, db_session) -> None:
    run, _ = _progress_fixture(db_session)

    negative = client.get(f"/api/v1/runs/{run.id}/progress/blocks/-1")
    too_large = client.get(f"/api/v1/runs/{run.id}/progress/blocks/2")
    missing = client.get("/api/v1/runs/missing/progress")

    assert negative.status_code == 422
    assert too_large.status_code == 422
    assert too_large.json()["detail"]["code"] == "progress_block_out_of_range"
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "run_not_found"


@pytest.mark.parametrize("invalid_position", [-1, 515])
def test_progress_index_fails_closed_for_response_outside_frozen_plan(
    client,
    db_session,
    invalid_position: int,
) -> None:
    run, _ = _progress_fixture(db_session)
    # -1 specifically covers SQLite's integer-division truncation toward block zero.
    question = Question(
        id=f"corrupt-progress-question-{invalid_position}",
        benchmark_id=run.benchmark_id,
        external_id="corrupt-progress-q",
        position=invalid_position,
        question_type=QuestionType.EXACT_MATCH,
        prompt="Question",
        reference_answer="one",
    )
    evidence = EvaluationResponse(
        id=f"corrupt-progress-response-{invalid_position}",
        run_id=run.id,
        question_id=question.id,
        raw_response="one",
        parsed_answer="one",
        reference_answer_snapshot="one",
        score=1,
        evaluator_name="exact_match_v1",
    )
    db_session.add_all([question, evidence])
    db_session.commit()

    response = client.get(f"/api/v1/runs/{run.id}/progress")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "run_progress_integrity_error"


def test_progress_index_fails_closed_for_cross_benchmark_response(client, db_session) -> None:
    run, _ = _progress_fixture(db_session)
    other_benchmark = Benchmark(
        id="cross-progress-benchmark",
        slug="cross-progress-benchmark",
        name="Cross progress benchmark",
        version="1.0.0",
        description="fixture",
        dimension="general",
        language="en",
        license="MIT",
        source="local",
        evaluator_type="exact_match",
        evaluator_config={},
        prompt_template={},
        dataset_hash="cross-progress-hash",
        question_count=1,
    )
    question = Question(
        id="cross-progress-question",
        benchmark_id=other_benchmark.id,
        external_id="cross-progress-q",
        position=1,
        question_type=QuestionType.EXACT_MATCH,
        prompt="Question",
        reference_answer="one",
    )
    evidence = EvaluationResponse(
        id="cross-progress-response",
        run_id=run.id,
        question_id=question.id,
        raw_response="one",
        parsed_answer="one",
        reference_answer_snapshot="one",
        score=1,
        evaluator_name="exact_match_v1",
    )
    db_session.add_all([other_benchmark, question, evidence])
    db_session.commit()

    response = client.get(f"/api/v1/runs/{run.id}/progress")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "run_progress_integrity_error"


def test_progress_openapi_and_logging_contract_are_explicit(client) -> None:
    openapi = client.get("/openapi.json").json()
    index_schema = openapi["components"]["schemas"]["EvaluationProgressIndex"]
    block_schema = openapi["components"]["schemas"]["EvaluationProgressBlock"]
    cell_schema = openapi["components"]["schemas"]["EvaluationProgressCell"]

    assert set(index_schema["required"]) == {
        "block_size",
        "total_questions",
        "completed_questions",
        "correct_questions",
        "error_questions",
        "score",
        "completion_rate",
        "answered_accuracy",
        "average_latency_ms",
        "known_input_tokens",
        "known_output_tokens",
        "input_token_reported_responses",
        "output_token_reported_responses",
        "known_estimated_cost",
        "estimated_cost_reported_responses",
        "blocks",
    }
    assert set(block_schema["required"]) == {"block_index", "items"}
    assert set(cell_schema["required"]) == {
        "position",
        "outcome",
        "score",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "estimated_cost",
        "error_type",
    }
    assert cell_schema["properties"]["position"]["minimum"] == 0
    assert "/api/v1/runs/{run_id}/progress" in openapi["paths"]
    assert "/api/v1/runs/{run_id}/progress/blocks/{block_index}" in openapi["paths"]
    assert normalize_request_path("/runs/{run_id}/progress") == "/runs/{run_id}/progress"
    assert (
        normalize_request_path("/runs/{run_id}/progress/blocks/{block_index}")
        == "/runs/{run_id}/progress/blocks/{block_index}"
    )
