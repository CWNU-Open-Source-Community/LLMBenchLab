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


def test_response_api_reports_partial_usage_totals_independently_of_pagination(
    client,
    db_session,
) -> None:
    model = Model(id="usage-model", name="Usage Mock", provider_type=ProviderType.MOCK)
    benchmark = Benchmark(
        id="usage-benchmark",
        slug="usage-benchmark",
        name="Usage benchmark",
        version="1.0.0",
        description="fixture",
        dimension="general",
        language="en",
        license="MIT",
        source="local",
        evaluator_type="exact_match",
        evaluator_config={},
        prompt_template={},
        dataset_hash="usage-hash",
        question_count=4,
    )
    questions = [
        Question(
            id=f"usage-question-{position}",
            benchmark_id=benchmark.id,
            external_id=f"usage-q{position}",
            position=position,
            question_type=QuestionType.EXACT_MATCH,
            prompt=f"Question {position}",
            reference_answer="one",
        )
        for position in range(4)
    ]
    run = EvaluationRun(
        id="usage-run",
        model_id=model.id,
        benchmark_id=benchmark.id,
        status=RunStatus.COMPLETED,
        model_parameters_snapshot={},
        benchmark_hash_snapshot=benchmark.dataset_hash,
        prompt_template_snapshot={},
        total_questions=4,
        completed_questions=4,
        correct_questions=4,
        input_tokens=None,
        output_tokens=None,
    )
    responses = [
        EvaluationResponse(
            run_id=run.id,
            question_id=question.id,
            raw_response="one",
            parsed_answer="one",
            reference_answer_snapshot="one",
            score=1,
            evaluator_name="exact_match_v1",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        for question, input_tokens, output_tokens in zip(
            questions,
            (10, 30, None, 0),
            (20, None, 40, None),
            strict=True,
        )
    ]
    db_session.add_all([model, benchmark, *questions, run, *responses])
    db_session.commit()

    first_page = client.get(f"/api/v1/runs/{run.id}/responses?offset=0&limit=1")
    last_page = client.get(f"/api/v1/runs/{run.id}/responses?offset=3&limit=1")

    assert first_page.status_code == last_page.status_code == 200
    expected_summary = {
        "total": 4,
        "known_input_tokens": 40,
        "known_output_tokens": 60,
        "input_token_reported_responses": 3,
        "output_token_reported_responses": 2,
    }
    for payload in (first_page.json(), last_page.json()):
        assert {key: payload[key] for key in expected_summary} == expected_summary
    assert first_page.json()["items"][0]["question_external_id"] == "usage-q0"
    assert last_page.json()["items"][0]["question_external_id"] == "usage-q3"

    run_payload = client.get(f"/api/v1/runs/{run.id}").json()
    assert run_payload["input_tokens"] is None
    assert run_payload["output_tokens"] is None


def test_response_usage_summary_openapi_fields_are_required_and_non_negative(
    client,
) -> None:
    schema = client.get("/openapi.json").json()["components"]["schemas"]["EvaluationResponseList"]
    fields = {
        "known_input_tokens",
        "known_output_tokens",
        "input_token_reported_responses",
        "output_token_reported_responses",
    }

    assert fields <= set(schema["required"])
    assert all(schema["properties"][field]["minimum"] == 0 for field in fields)
