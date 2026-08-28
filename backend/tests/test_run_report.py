"""Offline tests for complete, atomic Run report exports."""

from __future__ import annotations

import csv
import json
import stat
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

import app.reports.run_report as run_report_module
from app.core.constants import PROTOCOL_VERSION
from app.core.time import utc_now
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
from app.reports import (
    ReportDestinationExistsError,
    ReportValidationError,
    export_run_report,
)

_FAKE_SECRET = "sk-report-secret-value"


@pytest.fixture
def completed_report_run(client, db_session) -> str:
    """Create five persisted Responses with mixed groups and outcomes."""

    model = Model(
        name="Report Mock",
        provider_type=ProviderType.MOCK,
        enabled=True,
        input_price_per_million=Decimal(0),
        output_price_per_million=Decimal(0),
        default_parameters={},
    )
    benchmark = Benchmark(
        slug="report-fixture",
        name="Report Fixture",
        version="1.0.0",
        description="Offline report fixture",
        dimension="reasoning",
        language="en",
        license="MIT",
        source="test fixture",
        evaluator_type="builtin-objective",
        evaluator_config={
            "name": "builtin-objective",
            "version": "1.0",
            "mapping": {"multiple_choice": "multiple_choice_v1"},
        },
        prompt_template={"system": "Answer.", "user": "{prompt}\n{choices}"},
        dataset_hash="b" * 64,
        question_count=5,
        is_demo=False,
    )
    db_session.add_all([model, benchmark])
    db_session.flush()

    metadata_values = [
        {"category": "math", "private_author": "not exported"},
        {"category": "math"},
        {"category": "science"},
        {"domain": "reasoning"},
        {},
    ]
    questions: list[Question] = []
    for position, metadata in enumerate(metadata_values):
        question = Question(
            benchmark_id=benchmark.id,
            external_id=f"report-{position}",
            position=position,
            question_type=QuestionType.MULTIPLE_CHOICE,
            prompt=f"Question {position}",
            choices={"A": "first", "B": "second"},
            reference_answer="A",
            evaluator_config={},
            metadata_=metadata,
        )
        questions.append(question)
    db_session.add_all(questions)
    db_session.flush()

    now = utc_now()
    run = EvaluationRun(
        model_id=model.id,
        benchmark_id=benchmark.id,
        status=RunStatus.COMPLETED,
        protocol_version=PROTOCOL_VERSION,
        model_parameters_snapshot={
            "model": {
                "id": model.id,
                "name": "Frozen Report Mock",
                "adapter_type": "mock",
                "remote_model_name": None,
                "api_key_env": "REPORT_PROVIDER_API_KEY",
                "authorization": f"Bearer {_FAKE_SECRET}",
            },
            "benchmark": {
                "id": benchmark.id,
                "slug": benchmark.slug,
                "name": benchmark.name,
                "version": benchmark.version,
                "dataset_hash": benchmark.dataset_hash,
                "question_count": 5,
            },
            "generation": {"temperature": 0, "seed": 42},
            "execution": {"concurrency": 1},
        },
        benchmark_hash_snapshot=benchmark.dataset_hash,
        prompt_template_snapshot=dict(benchmark.prompt_template),
        code_commit_sha="a" * 40,
        total_questions=5,
        completed_questions=5,
        correct_questions=2,
        error_questions=2,
        score=40.0,
        completion_rate=60.0,
        answered_accuracy=2 / 3 * 100,
        average_latency_ms=25.0,
        input_tokens=5,
        output_tokens=10,
        estimated_cost=Decimal("0.005"),
        attempt_count=1,
        max_attempts=3,
        lease_token=1,
        started_at=now,
        finished_at=now,
    )
    db_session.add(run)
    db_session.flush()

    raw_responses = ["A", "B", None, "A", ""]
    scores = [1.0, 0.0, 0.0, 1.0, 0.0]
    errors = [
        (None, None),
        (None, None),
        (
            "authentication_error",
            f"Authorization: Bearer {_FAKE_SECRET}",
        ),
        (None, None),
        ("empty_response", f"api_key={_FAKE_SECRET}"),
    ]
    latencies = [10.0, 20.0, 30.0, 40.0, 25.0]
    for index, question in enumerate(questions):
        error_type, error_message = errors[index]
        db_session.add(
            EvaluationResponse(
                run_id=run.id,
                question_id=question.id,
                raw_response=raw_responses[index],
                parsed_answer=raw_responses[index] or None,
                reference_answer_snapshot="A",
                score=scores[index],
                evaluator_name="multiple_choice_v1",
                latency_ms=latencies[index],
                input_tokens=1,
                output_tokens=2,
                estimated_cost=Decimal("0.001"),
                provider_request_id=(
                    "provider-request-1" if index == 0 else _FAKE_SECRET if index == 2 else None
                ),
                returned_model=("vendor/model-v1" if index == 0 else None),
                system_fingerprint=("fp_123" if index == 0 else None),
                finish_reason=("stop" if index == 0 else None),
                http_attempt_count=(2 if index == 0 else None),
                error_type=error_type,
                error_message=error_message,
            )
        )
    db_session.commit()
    return run.id


def test_export_run_report_pages_all_evidence_and_partitions_groups(
    completed_report_run: str,
    db_session,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "report"

    exported = export_run_report(
        db_session,
        completed_report_run,
        destination,
        page_size=2,
        secret_values=[_FAKE_SECRET],
    )

    assert exported.directory == destination.resolve()
    assert exported.response_count == 5
    assert exported.group_count == 3
    assert exported.group_by == "category"
    assert stat.S_IMODE(exported.directory.stat().st_mode) == 0o700
    for path in (exported.summary_path, exported.groups_path, exported.responses_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    summary_lines = exported.summary_path.read_text(encoding="utf-8").splitlines()
    assert len(summary_lines) == 1
    summary = json.loads(summary_lines[0])
    assert summary["model"] == {
        "id": summary["model"]["id"],
        "name": "Frozen Report Mock",
        "provider_type": "mock",
        "remote_model_name": None,
    }
    assert summary["benchmark"]["slug"] == "report-fixture"
    assert summary["protocol_version"] == PROTOCOL_VERSION
    assert summary["dataset_hash"] == "b" * 64
    assert summary["code_commit_sha"] == "a" * 40
    assert summary["metrics"] == {
        "total_questions": 5,
        "completed_questions": 5,
        "correct_questions": 2,
        "error_questions": 2,
        "score": 40.0,
        "completion_rate": 60.0,
        "answered_accuracy": pytest.approx(2 / 3 * 100),
        "average_latency_ms": 25.0,
        "input_tokens": 5,
        "output_tokens": 10,
        "estimated_cost": 0.005,
    }
    assert summary["metrics_provenance"] == {
        "source": "persisted_responses_and_planned_questions",
        "persisted_run_fields_consistent": True,
        "persisted_run_field_differences": [],
    }
    assert summary["snapshots"]["model_parameters"]["model"]["api_key_env"] == (
        "REPORT_PROVIDER_API_KEY"
    )
    assert "authorization" not in summary["snapshots"]["model_parameters"]["model"]
    assert summary["group_by"] == "category"
    assert summary["files"] == {
        "summary": {"path": "summary.json", "line_count": 1, "record_count": 1},
        "groups": {"path": "groups.csv", "line_count": 4, "record_count": 3},
        "responses": {"path": "responses.jsonl", "line_count": 5, "record_count": 5},
    }

    with exported.groups_path.open(encoding="utf-8", newline="") as input_file:
        groups = {row["group_value"]: row for row in csv.DictReader(input_file)}
    assert set(groups) == {"math", "science", "__ungrouped__"}
    assert sum(int(row["planned_questions"]) for row in groups.values()) == 5
    assert sum(int(row["response_count"]) for row in groups.values()) == 5
    assert float(groups["math"]["score"]) == 50.0
    assert float(groups["math"]["completion_rate"]) == 100.0
    assert float(groups["math"]["answered_accuracy"]) == 50.0
    assert groups["science"]["answered_accuracy"] == ""
    assert float(groups["__ungrouped__"]["score"]) == 50.0
    assert float(groups["__ungrouped__"]["completion_rate"]) == 50.0
    assert float(groups["__ungrouped__"]["answered_accuracy"]) == 100.0

    response_lines = exported.responses_path.read_text(encoding="utf-8").splitlines()
    assert len(response_lines) == 5
    responses = [json.loads(line) for line in response_lines]
    assert [item["question_position"] for item in responses] == list(range(5))
    assert all(item["group_field"] == "category" for item in responses)
    assert {item["group_value"] for item in responses} == {
        "math",
        "science",
        "__ungrouped__",
    }
    assert all("metadata" not in item for item in responses)
    assert {
        field: responses[0][field]
        for field in (
            "provider_request_id",
            "returned_model",
            "system_fingerprint",
            "finish_reason",
            "http_attempt_count",
        )
    } == {
        "provider_request_id": "provider-request-1",
        "returned_model": "vendor/model-v1",
        "system_fingerprint": "fp_123",
        "finish_reason": "stop",
        "http_attempt_count": 2,
    }
    assert responses[2]["provider_request_id"] is None

    report_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (exported.summary_path, exported.groups_path, exported.responses_path)
    ).lower()
    assert _FAKE_SECRET.lower() not in report_text
    assert "authorization" not in report_text
    assert "bearer " not in report_text
    assert "private_author" not in report_text


def test_export_run_report_is_atomic_and_never_overwrites(
    completed_report_run: str,
    db_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    failed_destination = tmp_path / "failed-report"

    def fail_groups(*_args, **_kwargs):
        raise OSError("injected report write failure")

    monkeypatch.setattr(run_report_module, "_write_groups_csv", fail_groups)
    with pytest.raises(OSError, match="injected report write failure"):
        export_run_report(db_session, completed_report_run, failed_destination, page_size=2)

    assert not failed_destination.exists()
    assert not list(tmp_path.glob(".failed-report.tmp-*"))

    monkeypatch.undo()
    destination = tmp_path / "published-report"
    exported = export_run_report(db_session, completed_report_run, destination, page_size=2)
    original_summary = exported.summary_path.read_bytes()

    with pytest.raises(ReportDestinationExistsError):
        export_run_report(db_session, completed_report_run, destination, page_size=2)
    assert exported.summary_path.read_bytes() == original_summary


def test_export_run_report_rejects_non_whitelisted_group_dimension(
    completed_report_run: str,
    db_session,
    tmp_path: Path,
) -> None:
    with pytest.raises(ReportValidationError, match="group_by must be one of"):
        export_run_report(
            db_session,
            completed_report_run,
            tmp_path / "invalid-group",
            group_by="private_author",
        )

    assert not (tmp_path / "invalid-group").exists()


def test_failed_report_derives_one_consistent_metric_set_from_partial_evidence(
    completed_report_run: str,
    db_session,
    tmp_path: Path,
) -> None:
    run = db_session.get(EvaluationRun, completed_report_run)
    assert run is not None
    benchmark = db_session.get(Benchmark, run.benchmark_id)
    assert benchmark is not None
    questions = list(
        db_session.scalars(
            select(Question)
            .where(Question.benchmark_id == benchmark.id)
            .order_by(Question.position)
        )
    )
    responses = list(
        db_session.scalars(select(EvaluationResponse).where(EvaluationResponse.run_id == run.id))
    )
    for response in responses:
        if response.question_id != questions[0].id:
            db_session.delete(response)
    db_session.flush()
    for question in questions[2:]:
        db_session.delete(question)

    benchmark.question_count = 2
    run.status = RunStatus.FAILED
    run.total_questions = 2
    run.completed_questions = 0
    run.correct_questions = 0
    run.error_questions = 0
    run.score = 0.0
    run.completion_rate = 0.0
    run.answered_accuracy = None
    run.average_latency_ms = None
    run.input_tokens = None
    run.output_tokens = None
    run.estimated_cost = None
    run.finished_at = utc_now()
    db_session.commit()

    exported = export_run_report(
        db_session,
        run.id,
        tmp_path / "failed-partial-report",
        page_size=1,
    )

    summary = json.loads(exported.summary_path.read_text(encoding="utf-8"))
    assert summary["run"]["status"] == "failed"
    assert summary["metrics"] == {
        "total_questions": 2,
        "completed_questions": 1,
        "correct_questions": 1,
        "error_questions": 0,
        "score": 50.0,
        "completion_rate": 50.0,
        "answered_accuracy": 100.0,
        "average_latency_ms": 10.0,
        "input_tokens": 1,
        "output_tokens": 2,
        "estimated_cost": 0.001,
    }
    assert summary["metrics_provenance"]["source"] == ("persisted_responses_and_planned_questions")
    assert summary["metrics_provenance"]["persisted_run_fields_consistent"] is False
    assert set(summary["metrics_provenance"]["persisted_run_field_differences"]) == {
        "completed_questions",
        "correct_questions",
        "score",
        "completion_rate",
        "answered_accuracy",
        "average_latency_ms",
        "input_tokens",
        "output_tokens",
        "estimated_cost",
    }

    with exported.groups_path.open(encoding="utf-8", newline="") as input_file:
        groups = list(csv.DictReader(input_file))
    assert len(groups) == 1
    assert groups[0]["group_value"] == "math"
    assert int(groups[0]["planned_questions"]) == 2
    assert int(groups[0]["response_count"]) == 1
    assert int(groups[0]["correct_questions"]) == 1
    assert float(groups[0]["score"]) == summary["metrics"]["score"]
    assert float(groups[0]["completion_rate"]) == summary["metrics"]["completion_rate"]
    assert float(groups[0]["answered_accuracy"]) == summary["metrics"]["answered_accuracy"]

    responses_payload = [
        json.loads(line)
        for line in exported.responses_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(responses_payload) == summary["metrics"]["completed_questions"] == 1
    assert responses_payload[0]["score"] == 1.0
