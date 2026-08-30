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
from app.core.constants import MAX_GENERATION_TOKENS, MAX_READ_TIMEOUT_SECONDS
from app.db.session import SessionLocal
from app.models import (
    Benchmark,
    EvaluationResponse,
    EvaluationRun,
    GovernanceRunStatus,
    Model,
    Question,
    RunStatus,
)
from app.runners.evaluation_runner import EvaluationRunner
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
    assert info.json()["capabilities"]["providers"] == [
        "mock",
        "openai_compatible",
        "openai_responses",
        "anthropic_messages",
    ]

    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    assert set(openapi.json()["components"]["schemas"]["ProviderType"]["enum"]) == {
        "mock",
        "openai_compatible",
        "openai_responses",
        "anthropic_messages",
    }


def test_liveness_readiness_and_request_id_are_componentized(client, monkeypatch) -> None:
    liveness = client.get("/api/v1/live", headers={"X-Request-ID": "request-safe-1"})
    assert liveness.status_code == 200
    assert liveness.json()["status"] == "live"
    assert liveness.headers["X-Request-ID"]
    assert liveness.headers["X-Request-ID"] != "request-safe-1"

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
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Request-ID"] != "request-safe-error-1"
    assert response.json() == {
        "detail": {
            "code": "internal_server_error",
            "message": "An internal server error occurred",
        }
    }
    assert secret not in response.text
    failure = next(record for record in caplog.records if record.event == "api_request_failed")
    assert failure.request_id == response.headers["X-Request-ID"]
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


def test_request_log_normalizes_attacker_selected_http_method(client, caplog) -> None:
    secret_method = "SK-HTTP-METHOD-CANARY"
    caplog.set_level("INFO", logger="app.main")

    response = client.request(secret_method, "/api/v1/live")

    assert response.status_code == 405
    completion = next(
        record for record in reversed(caplog.records) if record.event == "api_request_completed"
    )
    assert completion.request_method == "unsupported"
    assert secret_method not in completion.getMessage()


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
        "managed_backlog": 0,
        "governance_delayed": 0,
        "governance_exhausted": 0,
        "active_provider_attempts": 0,
        "overdrawn_governance_scopes": 0,
        "total_attempts": 0,
        "total_failed_attempts": 0,
        "total_dispatches": 0,
        "worker_expected_processes": 1,
        "worker_registered_processes": 0,
        "worker_live_processes": 0,
        "worker_stalled_processes": 0,
        "worker_shortfall_processes": 1,
        "worker_stale_after_seconds": 60.0,
        "worker_last_seen_at": None,
        "worker_last_scan_at": None,
        "worker_last_claim_at": None,
        "worker_last_progress_at": None,
        "worker_last_lease_heartbeat_at": None,
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
    assert pending["managed_backlog"] == 1
    assert pending["running"] == pending["total_attempts"] == 0
    assert client.get("/api/v1/metrics/summary").status_code == 200

    with SessionLocal() as session, session.begin():
        delayed_run = session.get(EvaluationRun, run.json()["id"])
        assert delayed_run is not None
        delayed_run.governance_status = GovernanceRunStatus.DELAYED
        delayed_run.governance_not_before = datetime.now(UTC) + timedelta(hours=1)
    delayed = client.get("/api/v1/tasks/metrics").json()
    assert delayed["pending"] == delayed["managed_backlog"] == 1
    assert delayed["due_pending"] == 0
    assert delayed["governance_delayed"] == 1
    with SessionLocal() as session, session.begin():
        delayed_run = session.get(EvaluationRun, run.json()["id"])
        assert delayed_run is not None
        delayed_run.governance_status = GovernanceRunStatus.MANAGED
        delayed_run.governance_not_before = None

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
    assert after_cancel["total_dispatches"] == 1
    assert after_cancel["total_failed_attempts"] == 0


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


@pytest.mark.parametrize(
    ("provider_type", "endpoint"),
    [
        ("openai_compatible", "https://provider.example/v1/chat/completions"),
        ("openai_responses", "https://provider.example/v1/responses"),
        ("anthropic_messages", "https://provider.example/v1/messages"),
    ],
)
def test_remote_provider_protocols_persist_and_filter(
    client,
    provider_type: str,
    endpoint: str,
) -> None:
    created = client.post(
        "/api/v1/models",
        json={
            "name": f"Protocol {provider_type}",
            "provider_type": provider_type,
            "base_url": endpoint,
            "remote_model_name": "offline-model",
            "api_key_env": "PROTOCOL_PROVIDER_KEY",
        },
    )

    assert created.status_code == 201, created.text
    assert created.json()["provider_type"] == provider_type
    listed = client.get(f"/api/v1/models?provider_type={provider_type}")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == created.json()["id"]


@pytest.mark.parametrize(
    ("provider_type", "wrong_endpoint"),
    [
        ("openai_compatible", "https://provider.example/v1/responses"),
        ("openai_responses", "https://provider.example/v1/messages"),
        ("anthropic_messages", "https://provider.example/v1/chat/completions"),
    ],
)
def test_remote_provider_protocol_rejects_a_mismatched_known_endpoint(
    client,
    provider_type: str,
    wrong_endpoint: str,
) -> None:
    response = client.post(
        "/api/v1/models",
        json={
            "name": "Mismatched Protocol",
            "provider_type": provider_type,
            "base_url": wrong_endpoint,
            "remote_model_name": "offline-model",
            "api_key_env": "PROTOCOL_PROVIDER_KEY",
        },
    )

    assert response.status_code == 422
    assert "compatible root URL" in response.text


@pytest.mark.parametrize(
    ("provider_type", "default_parameters", "expected_message"),
    [
        ("openai_responses", {"seed": 42}, "does not support a non-null seed"),
        ("anthropic_messages", {"seed": 42}, "does not support a non-null seed"),
        ("anthropic_messages", {"max_tokens": None}, "requires a finite max_tokens"),
        ("anthropic_messages", {"temperature": 1.5}, "temperature must be between 0 and 1"),
    ],
)
def test_remote_provider_protocol_rejects_invalid_model_defaults(
    client,
    provider_type: str,
    default_parameters: dict[str, object],
    expected_message: str,
) -> None:
    response = client.post(
        "/api/v1/models",
        json={
            "name": f"Invalid defaults {provider_type}",
            "provider_type": provider_type,
            "base_url": "https://provider.example/v1",
            "remote_model_name": "offline-model",
            "api_key_env": "PROTOCOL_PROVIDER_KEY",
            "default_parameters": default_parameters,
        },
    )

    assert response.status_code == 422
    assert expected_message in response.text


def test_run_rejects_invalid_effective_provider_protocol_parameters_before_queueing(
    client,
) -> None:
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()
    responses_model = client.post(
        "/api/v1/models",
        json={
            "name": "Responses protocol run",
            "provider_type": "openai_responses",
            "base_url": "https://provider.example/v1",
            "remote_model_name": "responses-model",
            "api_key_env": "PROTOCOL_PROVIDER_KEY",
        },
    ).json()
    messages_model = client.post(
        "/api/v1/models",
        json={
            "name": "Messages protocol run",
            "provider_type": "anthropic_messages",
            "base_url": "https://provider.example/v1",
            "remote_model_name": "messages-model",
            "api_key_env": "PROTOCOL_PROVIDER_KEY",
        },
    ).json()

    responses_with_omitted_seed = client.post(
        "/api/v1/runs",
        json={"model_id": responses_model["id"], "benchmark_id": benchmark["id"]},
    )
    assert responses_with_omitted_seed.status_code == 202, responses_with_omitted_seed.text
    assert (
        responses_with_omitted_seed.json()["model_parameters_snapshot"]["generation"]["seed"]
        is None
    )
    assert responses_with_omitted_seed.json()["model_parameters_snapshot"]["generation"] == {
        "temperature": None,
        "top_p": None,
        "max_tokens": 256,
        "seed": None,
    }

    responses_with_explicit_seed = client.post(
        "/api/v1/runs",
        json={
            "model_id": responses_model["id"],
            "benchmark_id": benchmark["id"],
            "seed": 42,
        },
    )
    assert responses_with_explicit_seed.status_code == 422
    assert responses_with_explicit_seed.json()["detail"]["code"] == (
        "invalid_provider_generation_parameters"
    )

    accepted_responses = client.post(
        "/api/v1/runs",
        json={
            "model_id": responses_model["id"],
            "benchmark_id": benchmark["id"],
            "seed": None,
        },
    )
    assert accepted_responses.status_code == 202, accepted_responses.text
    assert (
        accepted_responses.json()["model_parameters_snapshot"]["model"]["adapter_type"]
        == "openai_responses"
    )

    messages_without_limit = client.post(
        "/api/v1/runs",
        json={
            "model_id": messages_model["id"],
            "benchmark_id": benchmark["id"],
            "seed": None,
            "max_tokens": None,
        },
    )
    assert messages_without_limit.status_code == 422
    assert messages_without_limit.json()["detail"]["code"] == (
        "invalid_provider_generation_parameters"
    )

    messages_with_invalid_temperature = client.post(
        "/api/v1/runs",
        json={
            "model_id": messages_model["id"],
            "benchmark_id": benchmark["id"],
            "seed": None,
            "temperature": 1.5,
        },
    )
    assert messages_with_invalid_temperature.status_code == 422
    assert messages_with_invalid_temperature.json()["detail"]["code"] == (
        "invalid_provider_generation_parameters"
    )

    accepted_messages = client.post(
        "/api/v1/runs",
        json={
            "model_id": messages_model["id"],
            "benchmark_id": benchmark["id"],
            "seed": None,
        },
    )
    assert accepted_messages.status_code == 202, accepted_messages.text
    assert accepted_messages.json()["model_parameters_snapshot"]["generation"] == {
        "temperature": None,
        "top_p": None,
        "max_tokens": 256,
        "seed": None,
    }
    assert accepted_messages.json()["model_parameters_snapshot"]["execution"]["retry_policy"][
        "retryable_status_codes"
    ] == [408, 429, 500, 502, 503, 504, 529]

    runner = EvaluationRunner(SessionLocal)
    responses_snapshot = runner._load_snapshots(accepted_responses.json()["id"])[0]
    messages_snapshot = runner._load_snapshots(accepted_messages.json()["id"])[0]
    assert responses_snapshot.provider_type == "openai_responses"
    assert messages_snapshot.provider_type == "anthropic_messages"


@pytest.mark.parametrize("provider_type", ["mock", "openai_compatible"])
@pytest.mark.parametrize("field", ["temperature", "top_p"])
def test_run_preserves_nonnullable_sampling_contract_for_legacy_protocols(
    client,
    provider_type: str,
    field: str,
) -> None:
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()
    model_payload: dict[str, object] = {
        "name": f"Legacy sampling {provider_type} {field}",
        "provider_type": provider_type,
    }
    if provider_type == "openai_compatible":
        model_payload.update(
            {
                "base_url": "https://provider.example/v1",
                "remote_model_name": "chat-model",
                "api_key_env": "PROTOCOL_PROVIDER_KEY",
            }
        )
    model = client.post("/api/v1/models", json=model_payload).json()

    response = client.post(
        "/api/v1/runs",
        json={
            "model_id": model["id"],
            "benchmark_id": benchmark["id"],
            field: None,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_provider_generation_parameters"


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
    for index, field in enumerate(("temperature", "top_p")):
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


def test_model_allows_provider_default_or_extended_max_tokens(client) -> None:
    for index, max_tokens in enumerate((None, MAX_GENERATION_TOKENS)):
        response = client.post(
            "/api/v1/models",
            json={
                "name": f"Output Default {index}",
                "provider_type": "mock",
                "default_parameters": {"max_tokens": max_tokens},
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["default_parameters"]["max_tokens"] == max_tokens


def test_run_snapshots_extended_output_budget_provider_default_and_read_timeout(client) -> None:
    model = client.post(
        "/api/v1/models",
        json={"name": "Budget Mock", "provider_type": "mock", "enabled": True},
    ).json()
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()

    extended = client.post(
        "/api/v1/runs",
        json={
            "model_id": model["id"],
            "benchmark_id": benchmark["id"],
            "max_tokens": MAX_GENERATION_TOKENS,
            "read_timeout_seconds": MAX_READ_TIMEOUT_SECONDS,
        },
    )
    assert extended.status_code == 202, extended.text
    extended_snapshot = extended.json()["model_parameters_snapshot"]
    assert extended_snapshot["generation"]["max_tokens"] == MAX_GENERATION_TOKENS
    assert extended_snapshot["execution"]["timeouts_seconds"]["read"] == (MAX_READ_TIMEOUT_SECONDS)

    provider_default = client.post(
        "/api/v1/runs",
        json={
            "model_id": model["id"],
            "benchmark_id": benchmark["id"],
            "max_tokens": None,
            "read_timeout_seconds": 300,
        },
    )
    assert provider_default.status_code == 202, provider_default.text
    provider_snapshot = provider_default.json()["model_parameters_snapshot"]
    assert provider_snapshot["generation"]["max_tokens"] is None
    assert provider_snapshot["execution"]["timeouts_seconds"]["read"] == 300

    inherited_model = client.post(
        "/api/v1/models",
        json={
            "name": "Provider-managed Output Mock",
            "provider_type": "mock",
            "enabled": True,
            "default_parameters": {"max_tokens": None},
        },
    ).json()
    inherited = client.post(
        "/api/v1/runs",
        json={"model_id": inherited_model["id"], "benchmark_id": benchmark["id"]},
    )
    assert inherited.status_code == 202, inherited.text
    inherited_snapshot = inherited.json()["model_parameters_snapshot"]
    assert inherited_snapshot["generation"]["max_tokens"] is None
    assert inherited_snapshot["execution"]["timeouts_seconds"]["read"] == 60


def test_run_preserves_protocol_defaults_and_explicit_null_overrides_model_default(client) -> None:
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()
    protocol_default_model = client.post(
        "/api/v1/models",
        json={"name": "Protocol Default Mock", "provider_type": "mock", "enabled": True},
    ).json()

    protocol_default = client.post(
        "/api/v1/runs",
        json={
            "model_id": protocol_default_model["id"],
            "benchmark_id": benchmark["id"],
        },
    )
    assert protocol_default.status_code == 202, protocol_default.text
    protocol_snapshot = protocol_default.json()["model_parameters_snapshot"]
    assert protocol_snapshot["generation"] == {
        "temperature": 0,
        "top_p": 1,
        "max_tokens": 256,
        "seed": 42,
    }
    assert protocol_snapshot["execution"]["timeouts_seconds"]["read"] == 60

    numeric_default_model = client.post(
        "/api/v1/models",
        json={
            "name": "Numeric Output Default Mock",
            "provider_type": "mock",
            "enabled": True,
            "default_parameters": {"max_tokens": 4096},
        },
    ).json()
    explicit_provider_default = client.post(
        "/api/v1/runs",
        json={
            "model_id": numeric_default_model["id"],
            "benchmark_id": benchmark["id"],
            "max_tokens": None,
        },
    )
    assert explicit_provider_default.status_code == 202, explicit_provider_default.text
    explicit_snapshot = explicit_provider_default.json()["model_parameters_snapshot"]
    assert explicit_snapshot["generation"]["max_tokens"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_tokens", MAX_GENERATION_TOKENS + 1),
        ("read_timeout_seconds", MAX_READ_TIMEOUT_SECONDS + 1),
        ("max_tokens", True),
        ("max_tokens", "8192"),
        ("read_timeout_seconds", True),
        ("read_timeout_seconds", "300"),
        ("input_token_reservation", True),
        ("input_token_reservation", 128.0),
        ("lifetime_request_budget", True),
        ("lifetime_request_budget", 10.0),
        ("lifetime_token_budget", True),
        ("lifetime_token_budget", 1024.0),
    ],
)
def test_run_rejects_invalid_output_budget_and_timeout_types_or_limits(
    client, field: str, value: object
) -> None:
    response = client.post(
        "/api/v1/runs",
        json={"model_id": "model-id", "benchmark_id": "benchmark-id", field: value},
    )
    assert response.status_code == 422
    assert field in response.text


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
