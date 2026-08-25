"""Versioned API and secret-safety integration tests."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import SQLAlchemyError

import app.api.v1.health as health_module
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import (
    Benchmark,
    EvaluationResponse,
    EvaluationRun,
    Model,
    Question,
    RunStatus,
)
from app.runners.run_leases import RunLeaseRepository
from app.task_queue import QueueUnavailable, RedisRunQueue


class _UnavailableQueue:
    async def ping(self) -> bool:
        raise QueueUnavailable("controlled test outage")


class _HalfOpenRedis:
    async def ping(self) -> bool:
        await asyncio.Event().wait()
        return True


def test_health_and_info_do_not_require_provider(client) -> None:
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    info = client.get("/api/v1/info")
    assert info.status_code == 200
    assert info.json()["protocol_version"] == "llmbenchlab-protocol-v1"


def test_liveness_readiness_and_request_id_are_componentized(client, monkeypatch) -> None:
    liveness = client.get("/api/v1/live", headers={"X-Request-ID": "request-safe-1"})
    assert liveness.status_code == 200
    assert liveness.json()["status"] == "live"
    assert liveness.headers["X-Request-ID"] == "request-safe-1"

    ready = client.get("/api/v1/ready")
    assert ready.status_code == 200
    assert {
        key: ready.json()[key]
        for key in (
            "status",
            "database",
            "schema",
            "queue",
            "accepting_runs",
            "database_reconciliation",
            "errors",
        )
    } == {
        "status": "ready",
        "database": "ok",
        "schema": "ok",
        "queue": "disabled",
        "accepting_runs": True,
        "database_reconciliation": "available",
        "errors": [],
    }

    monkeypatch.setattr(client.app.state, "run_queue", _UnavailableQueue())
    degraded = client.get("/api/v1/ready")
    assert degraded.status_code == 503
    assert degraded.json()["status"] == "degraded"
    assert degraded.json()["database"] == "ok"
    assert degraded.json()["schema"] == "ok"
    assert degraded.json()["queue"] == "unavailable"
    assert degraded.json()["accepting_runs"] is True
    assert degraded.json()["database_reconciliation"] == "available"
    assert degraded.json()["errors"] == ["queue_unavailable"]


def test_readiness_rejects_schema_mismatch_without_changing_health(client, monkeypatch) -> None:
    monkeypatch.setattr(health_module, "expected_database_heads", lambda: ("future-head",))

    ready = client.get("/api/v1/ready")

    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
    assert ready.json()["database"] == "ok"
    assert ready.json()["schema"] == "not_ready"
    assert ready.json()["accepting_runs"] is False
    assert ready.json()["errors"] == ["schema_not_ready"]
    assert client.get("/api/v1/health").status_code == 200


def test_liveness_never_probes_dependencies_and_database_failure_is_sanitized(
    client,
    monkeypatch,
) -> None:
    secret = "postgresql://user:password@database/private"

    def fail_database_session():
        raise SQLAlchemyError(secret)

    monkeypatch.setattr(health_module, "SessionLocal", fail_database_session)
    monkeypatch.setattr(client.app.state, "run_queue", _UnavailableQueue())

    liveness = client.get("/api/v1/live")
    assert liveness.status_code == 200
    assert liveness.json()["status"] == "live"
    assert secret not in liveness.text

    ready = client.get("/api/v1/ready")
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
    assert ready.json()["database"] == "unavailable"
    assert ready.json()["schema"] == "unavailable"
    assert ready.json()["errors"] == ["database_unavailable", "queue_unavailable"]
    assert secret not in ready.text


def test_readiness_bounds_half_open_redis_ping(client, monkeypatch) -> None:
    queue = RedisRunQueue(
        _HalfOpenRedis(),  # type: ignore[arg-type]
        stream="runs",
        consumer_group="workers",
        max_length=100,
        default_block_milliseconds=100,
        publish_timeout_seconds=0.01,
        operation_timeout_seconds=0.01,
    )
    monkeypatch.setattr(client.app.state, "run_queue", queue)

    started = time.monotonic()
    ready = client.get("/api/v1/ready")
    elapsed = time.monotonic() - started

    assert ready.status_code == 503
    assert ready.json()["status"] == "degraded"
    assert ready.json()["errors"] == ["queue_unavailable"]
    assert elapsed < 0.5


def test_readiness_database_timeout_is_sanitized_and_does_not_block_liveness(
    client,
    monkeypatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    secret = "postgresql://user:password@database/private"

    def half_open_database() -> tuple[str, str, list[str]]:
        entered.set()
        release.wait(timeout=1)
        raise SQLAlchemyError(secret)

    monkeypatch.setattr(health_module, "_database_readiness", half_open_database)
    monkeypatch.setattr(get_settings(), "readiness_database_timeout_seconds", 0.05)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            ready_future = executor.submit(client.get, "/api/v1/ready")
            assert entered.wait(timeout=0.5)

            started = time.monotonic()
            liveness = client.get("/api/v1/live")
            liveness_elapsed = time.monotonic() - started
            ready = ready_future.result(timeout=0.5)
    finally:
        release.set()

    assert liveness.status_code == 200
    assert liveness_elapsed < 0.2
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
    assert ready.json()["database"] == "unavailable"
    assert ready.json()["schema"] == "unavailable"
    assert ready.json()["errors"] == ["database_unavailable"]
    assert secret not in ready.text


def test_unhandled_api_error_is_sanitized_and_keeps_request_id(
    client,
    monkeypatch,
    caplog,
) -> None:
    secret = "provider-secret-must-not-be-reflected"

    def fail_timestamp():
        raise RuntimeError(secret)

    monkeypatch.setattr(health_module, "utc_now", fail_timestamp)
    caplog.set_level("ERROR", logger="app.main")

    response = client.get(
        "/api/v1/live",
        headers={"X-Request-ID": "request-safe-error-1"},
    )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "request-safe-error-1"
    assert response.json() == {
        "detail": {
            "code": "internal_server_error",
            "message": "An internal server error occurred",
        }
    }
    assert secret not in response.text
    failure = next(record for record in caplog.records if record.event == "api_request_failed")
    assert failure.request_id == "request-safe-error-1"
    assert failure.request_path == "/live"
    assert failure.error_code == "api_error:RuntimeError"
    assert failure.result == "internal_server_error"
    assert secret not in failure.getMessage()


def test_request_log_uses_route_template_instead_of_unmatched_user_path(client, caplog) -> None:
    secret_path = "/not-found/sk-secret-value-must-not-enter-application-log"
    caplog.set_level("INFO", logger="app.main")

    response = client.get(secret_path)

    assert response.status_code == 404
    completion = next(
        record for record in reversed(caplog.records) if record.event == "api_request_completed"
    )
    assert completion.request_path == "<unmatched>"
    assert "sk-secret-value" not in completion.getMessage()


def test_task_metrics_are_database_derived_and_do_not_replace_dashboard_metrics(client) -> None:
    empty = client.get("/api/v1/tasks/metrics")
    assert empty.status_code == 200
    assert {key: value for key, value in empty.json().items() if key != "timestamp"} == {
        "pending": 0,
        "due_pending": 0,
        "running": 0,
        "expired_running": 0,
        "active_cancellation_requests": 0,
        "retry_scheduled": 0,
        "dead_lettered": 0,
        "runs_with_queue_notification_error": 0,
        "total_attempts": 0,
    }
    model = client.post(
        "/api/v1/models",
        json={"name": "Metrics Mock", "provider_type": "mock", "enabled": True},
    )
    benchmark = client.post("/api/v1/benchmarks/reload-demo")
    run = client.post(
        "/api/v1/runs",
        json={"model_id": model.json()["id"], "benchmark_id": benchmark.json()["id"]},
    )
    assert run.status_code == 202

    pending = client.get("/api/v1/tasks/metrics").json()
    assert pending["pending"] == pending["due_pending"] == 1
    assert pending["running"] == pending["total_attempts"] == 0
    assert client.get("/api/v1/metrics/summary").status_code == 200

    settings = get_settings()
    repository = RunLeaseRepository(
        SessionLocal,
        lease_for=timedelta(seconds=settings.worker_lease_seconds),
    )
    assert repository.claim(run.json()["id"], owner="worker-metrics") is not None
    cancelled = client.post(f"/api/v1/runs/{run.json()['id']}/cancel")
    assert cancelled.status_code == 200
    after_cancel = client.get("/api/v1/tasks/metrics").json()
    assert after_cancel["pending"] == after_cancel["due_pending"] == 0
    assert after_cancel["running"] == 1
    assert after_cancel["active_cancellation_requests"] == 1


def test_model_crud_pagination_and_secret_value_not_returned(client) -> None:
    os.environ["TEST_PROVIDER_KEY"] = "secret-value-must-never-appear"
    created = client.post(
        "/api/v1/models",
        json={
            "name": "Compatible Test",
            "provider_type": "openai_compatible",
            "base_url": "https://models.invalid/v1",
            "remote_model_name": "offline-test-model",
            "api_key_env": "TEST_PROVIDER_KEY",
            "enabled": True,
        },
    )
    assert created.status_code == 201
    assert created.json()["api_key_env"] == "TEST_PROVIDER_KEY"
    assert created.json()["input_price_per_million"] is None
    assert created.json()["output_price_per_million"] is None
    assert "secret-value-must-never-appear" not in created.text

    model_id = created.json()["id"]
    updated = client.patch(f"/api/v1/models/{model_id}", json={"enabled": False})
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    listed = client.get("/api/v1/models?offset=0&limit=1")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == model_id
    deleted = client.delete(f"/api/v1/models/{model_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/models/{model_id}").status_code == 404


def test_openai_model_provider_fields_are_validated(client) -> None:
    response = client.post(
        "/api/v1/models",
        json={"name": "Incomplete", "provider_type": "openai_compatible"},
    )
    assert response.status_code == 422
    assert "base_url" in response.text


def test_mock_model_rejects_remote_connection_fields(client) -> None:
    response = client.post(
        "/api/v1/models",
        json={
            "name": "Unsafe Mock",
            "provider_type": "mock",
            "base_url": "https://models.invalid/v1",
            "remote_model_name": "not-used",
            "api_key_env": "TEST_PROVIDER_KEY",
        },
    )
    assert response.status_code == 422
    assert "mock requires empty" in response.text


def test_model_rejects_secret_bypass_fields_without_reflecting_values(client) -> None:
    fake_secret = "credential-value-that-must-not-be-reflected"
    sensitive_keys = (
        "token",
        "api_token",
        "apiToken",
        "clientSecret",
        "private_key",
        "credential",
        "x_token",
        "nested",
    )
    for index, sensitive_key in enumerate(sensitive_keys):
        default_parameter_response = client.post(
            "/api/v1/models",
            json={
                "name": f"Unsafe Parameters {index}",
                "provider_type": "mock",
                "default_parameters": {sensitive_key: fake_secret},
            },
        )
        assert default_parameter_response.status_code == 422
        assert "only supports" in default_parameter_response.text
        assert fake_secret not in default_parameter_response.text

    query_response = client.post(
        "/api/v1/models",
        json={
            "name": "Unsafe URL",
            "provider_type": "openai_compatible",
            "base_url": f"https://models.invalid/v1?token={fake_secret}",
            "remote_model_name": "test-model",
            "api_key_env": "TEST_PROVIDER_KEY",
        },
    )
    assert query_response.status_code == 422
    assert "query parameters" in query_response.text
    assert fake_secret not in query_response.text


@pytest.mark.parametrize("price_literal", ["Infinity", "-Infinity", "NaN"])
def test_model_rejects_non_finite_prices(client, price_literal: str) -> None:
    response = client.post(
        "/api/v1/models",
        content=(
            '{"name":"Invalid Price","provider_type":"mock",'
            f'"input_price_per_million":{price_literal}}}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "finite number" in response.text


@pytest.mark.parametrize("number_literal", ["Infinity", "-Infinity", "NaN"])
def test_model_rejects_non_finite_default_parameters(client, number_literal: str) -> None:
    response = client.post(
        "/api/v1/models",
        content=(
            '{"name":"Invalid Defaults","provider_type":"mock",'
            f'"default_parameters":{{"temperature":{number_literal}}}}}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "finite number" in response.text


def test_model_rejects_null_for_nonnullable_default_parameters(client) -> None:
    for index, field in enumerate(("temperature", "top_p", "max_tokens")):
        response = client.post(
            "/api/v1/models",
            json={
                "name": f"Null Default {index}",
                "provider_type": "mock",
                "default_parameters": {field: None},
            },
        )
        assert response.status_code == 422
        assert f"default_parameters.{field} must not be null" in response.text


def test_run_concurrency_cannot_exceed_runner_limit(client) -> None:
    response = client.post(
        "/api/v1/runs",
        json={
            "model_id": "model-id",
            "benchmark_id": "benchmark-id",
            "concurrency": 5,
        },
    )
    assert response.status_code == 422
    assert "concurrency" in response.text


def test_cancel_retrying_run_clears_backoff_and_returns_aggregated_terminal_state(
    client,
    db_session,
) -> None:
    model = Model(id="cancel-model", name="Cancel Mock", provider_type="mock")
    benchmark = Benchmark(
        id="cancel-benchmark",
        slug="cancel-benchmark",
        name="Cancel benchmark",
        version="1.0.0",
        description="fixture",
        dimension="general",
        language="en",
        license="MIT",
        source="local",
        evaluator_type="exact_match",
        evaluator_config={},
        prompt_template={},
        dataset_hash="cancel-hash",
        question_count=1,
    )
    question = Question(
        id="cancel-question",
        benchmark_id=benchmark.id,
        external_id="q1",
        position=0,
        question_type="exact_match",
        prompt="One?",
        reference_answer="one",
    )
    run = EvaluationRun(
        id="cancel-run",
        model_id=model.id,
        benchmark_id=benchmark.id,
        status=RunStatus.PENDING,
        model_parameters_snapshot={},
        benchmark_hash_snapshot=benchmark.dataset_hash,
        prompt_template_snapshot={},
        total_questions=1,
        completed_questions=1,
        attempt_count=1,
        max_attempts=2,
        next_attempt_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    response = EvaluationResponse(
        run_id=run.id,
        question_id=question.id,
        raw_response="one",
        parsed_answer="one",
        reference_answer_snapshot="one",
        score=1,
        evaluator_name="exact_match_v1",
    )
    db_session.add_all([model, benchmark, question, run, response])
    db_session.commit()

    cancelled = client.post(f"/api/v1/runs/{run.id}/cancel")

    assert cancelled.status_code == 200
    payload = cancelled.json()
    assert payload["status"] == "cancelled"
    assert payload["cancellation_requested"] is True
    assert payload["next_attempt_at"] is None
    assert payload["completed_questions"] == payload["correct_questions"] == 1
    assert payload["score"] == payload["completion_rate"] == 100.0
    db_session.expire_all()
    persisted = db_session.get(EvaluationRun, run.id)
    assert persisted is not None and persisted.lease_owner is None


def test_database_crud_round_trip(db_session) -> None:
    row = Model(name="Database Mock", provider_type="mock", default_parameters={})
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    assert db_session.get(Model, row.id).name == "Database Mock"
