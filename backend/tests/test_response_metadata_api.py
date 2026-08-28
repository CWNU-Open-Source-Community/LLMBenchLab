"""Provider metadata exposure remains bounded and secret-minimized."""

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


def test_response_api_exposes_safe_provider_metadata_and_nulls_unsafe_values(
    client,
    db_session,
) -> None:
    model = Model(id="metadata-model", name="Metadata Mock", provider_type=ProviderType.MOCK)
    benchmark = Benchmark(
        id="metadata-benchmark",
        slug="metadata-benchmark",
        name="Metadata benchmark",
        version="1.0.0",
        description="fixture",
        dimension="general",
        language="en",
        license="MIT",
        source="local",
        evaluator_type="exact_match",
        evaluator_config={},
        prompt_template={},
        dataset_hash="metadata-hash",
        question_count=2,
    )
    questions = [
        Question(
            id=f"metadata-question-{position}",
            benchmark_id=benchmark.id,
            external_id=f"q{position}",
            position=position,
            question_type=QuestionType.EXACT_MATCH,
            prompt=f"Question {position}",
            reference_answer="one",
        )
        for position in range(2)
    ]
    run = EvaluationRun(
        id="metadata-run",
        model_id=model.id,
        benchmark_id=benchmark.id,
        status=RunStatus.COMPLETED,
        model_parameters_snapshot={},
        benchmark_hash_snapshot=benchmark.dataset_hash,
        prompt_template_snapshot={},
        total_questions=2,
        completed_questions=2,
    )
    safe = EvaluationResponse(
        run_id=run.id,
        question_id=questions[0].id,
        raw_response="one",
        parsed_answer="one",
        reference_answer_snapshot="one",
        score=1,
        evaluator_name="exact_match_v1",
        provider_request_id="provider-request_123",
        returned_model="vendor/model-v1",
        system_fingerprint="fp_123",
        finish_reason="stop",
        http_attempt_count=2,
    )
    unsafe = EvaluationResponse(
        run_id=run.id,
        question_id=questions[1].id,
        raw_response="one",
        parsed_answer="one",
        reference_answer_snapshot="one",
        score=1,
        evaluator_name="exact_match_v1",
        provider_request_id="Authorization:Bearer",
        returned_model="model with spaces",
        system_fingerprint="sk-secretvalue123",
        finish_reason="[REDACTED]",
        http_attempt_count=1,
    )
    db_session.add_all([model, benchmark, *questions, run, safe, unsafe])
    db_session.commit()

    response = client.get(f"/api/v1/runs/{run.id}/responses?limit=100")

    assert response.status_code == 200
    items = response.json()["items"]
    assert {
        field: items[0][field]
        for field in (
            "provider_request_id",
            "returned_model",
            "system_fingerprint",
            "finish_reason",
            "http_attempt_count",
        )
    } == {
        "provider_request_id": "provider-request_123",
        "returned_model": "vendor/model-v1",
        "system_fingerprint": "fp_123",
        "finish_reason": "stop",
        "http_attempt_count": 2,
    }
    assert {
        field: items[1][field]
        for field in (
            "provider_request_id",
            "returned_model",
            "system_fingerprint",
            "finish_reason",
        )
    } == {
        "provider_request_id": None,
        "returned_model": None,
        "system_fingerprint": None,
        "finish_reason": None,
    }
    assert items[1]["http_attempt_count"] == 1
    assert "sk-secretvalue123" not in response.text
