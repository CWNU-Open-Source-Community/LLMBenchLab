"""Bounded, fail-closed Prometheus exporter contract tests."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

import app.api.v1.observability as observability_api
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.governance.audit import append_audit_event
from app.models import (
    Benchmark,
    EvaluationRun,
    Model,
    ProviderType,
    RunStatus,
    WorkerProcess,
)
from app.observability import (
    METRICS_AUDIT_EVENT_LIMIT,
    TASK_EVENT_TYPES,
    MetricsObservationLimitExceeded,
    collect_task_history,
    configure_read_snapshot,
    database_clock,
)
from app.observability.prometheus import PrometheusRenderingError, render_prometheus
from app.task_queue import QueueUnavailable


class _UnavailableQueue:
    async def ping(self) -> bool:
        raise QueueUnavailable("redis://user:queue-secret@queue/private")


_EXPECTED_FAMILIES = (
    "llmbenchlab_runs_pending",
    "llmbenchlab_runs_due_pending",
    "llmbenchlab_runs_running",
    "llmbenchlab_runs_expired_running",
    "llmbenchlab_runs_cancellation_requested",
    "llmbenchlab_runs_retry_scheduled",
    "llmbenchlab_runs_dead_lettered",
    "llmbenchlab_runs_queue_notification_error",
    "llmbenchlab_runs_managed_backlog",
    "llmbenchlab_runs_governance_delayed",
    "llmbenchlab_runs_governance_exhausted",
    "llmbenchlab_provider_attempts_active",
    "llmbenchlab_governance_scopes_overdrawn",
    "llmbenchlab_run_lease_acquisitions",
    "llmbenchlab_run_failed_attempts",
    "llmbenchlab_run_dispatches",
    "llmbenchlab_run_expired_lease_oldest_age_seconds",
    "llmbenchlab_audit_events_window",
    "llmbenchlab_audit_event_window_seconds",
    "llmbenchlab_metrics_audit_events_scanned",
    "llmbenchlab_metrics_audit_event_limit",
    "llmbenchlab_run_latency_quantile_seconds",
    "llmbenchlab_run_latency_samples",
    "llmbenchlab_run_latency_truncated",
    "llmbenchlab_run_latency_window_seconds",
    "llmbenchlab_metrics_latency_sample_limit",
    "llmbenchlab_queue_configured",
    "llmbenchlab_queue_available",
    "llmbenchlab_worker_processes",
    "llmbenchlab_worker_expected_minimum",
    "llmbenchlab_worker_shortfall",
    "llmbenchlab_worker_activity_observed",
    "llmbenchlab_worker_activity_oldest_age_seconds",
    "llmbenchlab_worker_stale_threshold_seconds",
    "llmbenchlab_run_recovery_alert_threshold_seconds",
    "llmbenchlab_metrics_snapshot_unixtime_seconds",
)


def _sample_value(body: str, sample: str) -> float:
    prefix = sample + " "
    line = next(line for line in body.splitlines() if line.startswith(prefix))
    return float(line.removeprefix(prefix))


def _database_now() -> datetime:
    with SessionLocal() as session:
        value = session.scalar(select(func.current_timestamp()))
    assert isinstance(value, datetime)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def test_prometheus_empty_snapshot_has_fixed_text_contract(client) -> None:
    response = client.get("/api/v1/metrics/prometheus")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert "\r" not in response.text
    assert response.text.endswith("\n")
    assert not response.text.endswith("\n\n")
    assert "# TYPE " in response.text
    assert " counter\n" not in response.text
    assert "_total" not in response.text
    assert [
        line.split()[2] for line in response.text.splitlines() if line.startswith("# TYPE ")
    ] == list(_EXPECTED_FAMILIES)
    assert _sample_value(response.text, "llmbenchlab_runs_pending") == 0
    assert _sample_value(response.text, "llmbenchlab_queue_configured") == 0
    assert _sample_value(response.text, "llmbenchlab_queue_available") == 0
    assert _sample_value(response.text, "llmbenchlab_worker_expected_minimum") == 1
    assert _sample_value(response.text, "llmbenchlab_worker_shortfall") == 1
    assert _sample_value(response.text, 'llmbenchlab_worker_processes{state="registered"}') == 0
    assert _sample_value(response.text, 'llmbenchlab_worker_processes{state="live"}') == 0
    assert _sample_value(response.text, 'llmbenchlab_worker_processes{state="stalled"}') == 0
    for event_type in TASK_EVENT_TYPES:
        assert (
            _sample_value(
                response.text,
                f'llmbenchlab_audit_events_window{{event_type="{event_type}"}}',
            )
            == 0
        )
    for activity in ("scan", "claim", "lease_heartbeat", "progress"):
        assert (
            _sample_value(
                response.text,
                f'llmbenchlab_worker_activity_observed{{activity="{activity}"}}',
            )
            == 0
        )
        assert (
            f'llmbenchlab_worker_activity_oldest_age_seconds{{activity="{activity}"}}'
            not in response.text
        )
    assert "llmbenchlab_run_latency_quantile_seconds{" not in response.text


def test_prometheus_rejects_dynamic_query_parameters(client) -> None:
    response = client.get("/api/v1/metrics/prometheus?window=custom")

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "metrics_query_parameters_not_allowed",
            "message": "Metrics scrape does not accept query parameters",
        }
    }


def test_prometheus_validates_window_events_and_exposes_worker_aggregate(
    client,
    db_session,
) -> None:
    now = _database_now()
    secret_identity = "worker-secret-identity"
    db_session.add(
        WorkerProcess(
            generation_id="11111111-1111-4111-8111-111111111111",
            worker_id=secret_identity,
            started_at=now - timedelta(seconds=30),
            last_seen_at=now - timedelta(seconds=1),
            last_scan_at=now - timedelta(seconds=5),
            last_claim_at=now - timedelta(seconds=4),
            last_progress_at=now - timedelta(seconds=3),
            last_lease_heartbeat_at=now - timedelta(seconds=2),
        )
    )
    append_audit_event(
        db_session,
        event_key="metrics:dead-letter:inside",
        event_type="run_dead_lettered",
        occurred_at=now - timedelta(seconds=10),
        payload={"failed_attempt_count": 3, "reason": "worker_error"},
        correlation_id="metrics-secret-run",
        run_id="metrics-secret-run",
        model_id="metrics-secret-model",
    )
    append_audit_event(
        db_session,
        event_key="metrics:dead-letter:outside",
        event_type="run_dead_lettered",
        occurred_at=now - timedelta(seconds=901),
        payload={"failed_attempt_count": 3, "reason": "worker_error"},
        correlation_id="metrics-outside-run",
        run_id="metrics-outside-run",
    )
    db_session.commit()

    response = client.get("/api/v1/metrics/prometheus")

    assert response.status_code == 200
    assert (
        _sample_value(
            response.text,
            'llmbenchlab_audit_events_window{event_type="run_dead_lettered"}',
        )
        == 1
    )
    assert _sample_value(response.text, "llmbenchlab_metrics_audit_events_scanned") == 1
    assert _sample_value(response.text, "llmbenchlab_metrics_audit_event_limit") == 50_000
    assert _sample_value(response.text, 'llmbenchlab_worker_processes{state="registered"}') == 1
    assert _sample_value(response.text, 'llmbenchlab_worker_processes{state="live"}') == 1
    assert _sample_value(response.text, 'llmbenchlab_worker_processes{state="stalled"}') == 0
    assert _sample_value(response.text, "llmbenchlab_worker_shortfall") == 0
    for activity in ("scan", "claim", "lease_heartbeat", "progress"):
        assert (
            _sample_value(
                response.text,
                f'llmbenchlab_worker_activity_observed{{activity="{activity}"}}',
            )
            == 1
        )
        assert (
            f'llmbenchlab_worker_activity_oldest_age_seconds{{activity="{activity}"}}'
            in response.text
        )
    assert secret_identity not in response.text
    assert "metrics-secret-run" not in response.text
    assert "metrics-secret-model" not in response.text


def test_prometheus_worker_oldest_age_and_json_latest_timestamp_are_distinct(
    client,
    db_session,
) -> None:
    now = _database_now()
    recent_scan = now - timedelta(seconds=2)
    old_scan = now - timedelta(seconds=20)
    db_session.add_all(
        [
            WorkerProcess(
                generation_id="21111111-1111-4111-8111-111111111111",
                worker_id="metrics-worker-recent",
                started_at=now - timedelta(seconds=40),
                last_seen_at=now - timedelta(seconds=1),
                last_scan_at=recent_scan,
            ),
            WorkerProcess(
                generation_id="31111111-1111-4111-8111-111111111111",
                worker_id="metrics-worker-oldest",
                started_at=now - timedelta(seconds=40),
                last_seen_at=now - timedelta(seconds=1),
                last_scan_at=old_scan,
            ),
        ]
    )
    db_session.commit()

    task_metrics = client.get("/api/v1/tasks/metrics")
    prometheus = client.get("/api/v1/metrics/prometheus")

    assert task_metrics.status_code == 200
    assert task_metrics.json()["worker_registered_processes"] == 2
    assert task_metrics.json()["worker_live_processes"] == 2
    assert datetime.fromisoformat(task_metrics.json()["worker_last_scan_at"]) == recent_scan
    assert prometheus.status_code == 200
    assert (
        _sample_value(
            prometheus.text,
            'llmbenchlab_worker_activity_oldest_age_seconds{activity="scan"}',
        )
        >= 20
    )


def test_prometheus_run_latency_uses_the_fixed_one_hour_window(client, db_session) -> None:
    now = _database_now()
    model = Model(
        id="metrics-latency-model", name="Metrics latency", provider_type=ProviderType.MOCK
    )
    benchmark = Benchmark(
        id="metrics-latency-benchmark",
        slug="metrics-latency-benchmark",
        name="Metrics latency benchmark",
        version="1.0.0",
        description="fixture",
        dimension="general",
        language="en",
        license="MIT",
        source="local",
        evaluator_type="exact_match",
        evaluator_config={},
        prompt_template={},
        dataset_hash="metrics-latency-hash",
        question_count=1,
    )
    db_session.add_all(
        [
            model,
            benchmark,
            EvaluationRun(
                id="metrics-latency-inside",
                model_id=model.id,
                benchmark_id=benchmark.id,
                status=RunStatus.COMPLETED,
                model_parameters_snapshot={},
                benchmark_hash_snapshot=benchmark.dataset_hash,
                prompt_template_snapshot={},
                created_at=now - timedelta(minutes=50),
                started_at=now - timedelta(minutes=45),
                finished_at=now - timedelta(minutes=40),
            ),
            EvaluationRun(
                id="metrics-latency-outside",
                model_id=model.id,
                benchmark_id=benchmark.id,
                status=RunStatus.COMPLETED,
                model_parameters_snapshot={},
                benchmark_hash_snapshot=benchmark.dataset_hash,
                prompt_template_snapshot={},
                created_at=now - timedelta(minutes=70),
                started_at=now - timedelta(minutes=65),
                finished_at=now - timedelta(minutes=61),
            ),
        ]
    )
    db_session.commit()

    response = client.get("/api/v1/metrics/prometheus")

    assert response.status_code == 200
    for phase in ("queue", "execution", "end_to_end"):
        assert (
            _sample_value(response.text, f'llmbenchlab_run_latency_samples{{phase="{phase}"}}') == 1
        )
        assert (
            _sample_value(response.text, f'llmbenchlab_run_latency_truncated{{phase="{phase}"}}')
            == 0
        )
    assert (
        _sample_value(
            response.text,
            'llmbenchlab_run_latency_quantile_seconds{phase="queue",quantile="0.5"}',
        )
        == 300
    )
    assert (
        _sample_value(
            response.text,
            'llmbenchlab_run_latency_quantile_seconds{phase="execution",quantile="0.5"}',
        )
        == 300
    )
    assert (
        _sample_value(
            response.text,
            'llmbenchlab_run_latency_quantile_seconds{phase="end_to_end",quantile="0.5"}',
        )
        == 600
    )


def test_prometheus_expired_lease_age_is_current_right_censored_fact(client) -> None:
    model = client.post(
        "/api/v1/models",
        json={"name": "Expired lease model", "provider_type": "mock", "enabled": True},
    )
    benchmark = client.post("/api/v1/benchmarks/reload-demo")
    run = client.post(
        "/api/v1/runs",
        json={"model_id": model.json()["id"], "benchmark_id": benchmark.json()["id"]},
    )
    assert run.status_code == 202
    now = _database_now()
    with SessionLocal() as session, session.begin():
        persisted = session.get(EvaluationRun, run.json()["id"])
        assert persisted is not None
        persisted.status = RunStatus.RUNNING
        persisted.lease_owner = "metrics-expired-worker"
        persisted.lease_token = 1
        persisted.lease_expires_at = now - timedelta(seconds=5)
        persisted.heartbeat_at = now - timedelta(seconds=10)
        persisted.started_at = now - timedelta(seconds=30)

    response = client.get("/api/v1/metrics/prometheus")

    assert response.status_code == 200
    assert _sample_value(response.text, "llmbenchlab_runs_expired_running") == 1
    assert (
        _sample_value(
            response.text,
            "llmbenchlab_run_expired_lease_oldest_age_seconds",
        )
        >= 5
    )


def test_prometheus_queue_failure_is_non_authoritative_and_sanitized(
    client,
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setattr(client.app.state, "run_queue", _UnavailableQueue())
    caplog.set_level("WARNING", logger="app.api.v1.observability")

    response = client.get("/api/v1/metrics/prometheus")

    assert response.status_code == 200
    assert _sample_value(response.text, "llmbenchlab_queue_configured") == 1
    assert _sample_value(response.text, "llmbenchlab_queue_available") == 0
    assert "queue-secret" not in response.text
    assert "queue-secret" not in caplog.text
    assert any(record.event == "metrics_queue_unavailable" for record in caplog.records)


def test_prometheus_audit_corruption_fails_whole_scrape_without_reflection(
    client,
    db_session,
    caplog,
) -> None:
    marker = "sk-metrics-corruption-never-reflect"
    event = append_audit_event(
        db_session,
        event_key="metrics:corrupt:event",
        event_type="run_claimed",
        occurred_at=_database_now() - timedelta(seconds=1),
        payload={"dispatch_count": 1},
        run_id="metrics-corrupt-run",
        worker_id="metrics-corrupt-worker",
        attempt=1,
        lease_token=1,
    )
    event.payload_hash = marker + "x" * (64 - len(marker))
    db_session.commit()
    caplog.set_level("ERROR", logger="app.api.v1.observability")

    response = client.get("/api/v1/metrics/prometheus")

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "audit_event_integrity_error",
            "message": "A retained audit event failed integrity validation",
        }
    }
    assert marker not in response.text
    assert marker not in caplog.text


@pytest.mark.parametrize("damage", ["payload_json", "occurred_at"])
def test_prometheus_and_history_classify_invalid_audit_storage_as_integrity_error(
    client,
    db_session,
    caplog,
    damage,
) -> None:
    marker = "sk-invalid-audit-json-never-reflect"
    occurred_at = _database_now() - timedelta(seconds=1)
    event = append_audit_event(
        db_session,
        event_key="metrics:corrupt:json",
        event_type="run_claimed",
        occurred_at=occurred_at,
        payload={"dispatch_count": 1},
        run_id="metrics-corrupt-json-run",
        worker_id="metrics-corrupt-json-worker",
        attempt=1,
        lease_token=1,
    )
    event_id = event.id
    db_session.commit()
    if damage == "payload_json":
        db_session.connection().exec_driver_sql(
            "UPDATE audit_events SET payload = ? WHERE id = ?",
            ("{" + marker, event_id),
        )
    else:
        malformed_timestamp = occurred_at.replace(tzinfo=None).isoformat(sep=" ") + marker
        db_session.connection().exec_driver_sql(
            "UPDATE audit_events SET occurred_at = ? WHERE id = ?",
            (malformed_timestamp, event_id),
        )
    db_session.commit()
    caplog.set_level("ERROR")

    prometheus = client.get("/api/v1/metrics/prometheus")
    history = client.get("/api/v1/tasks/history?window_hours=1")

    assert prometheus.status_code == 500
    assert prometheus.json() == {
        "detail": {
            "code": "audit_event_integrity_error",
            "message": "A retained audit event failed integrity validation",
        }
    }
    assert history.status_code == 500
    assert history.json() == {
        "detail": {
            "code": "audit_event_integrity_error",
            "message": "A retained audit event failed integrity validation",
        }
    }
    assert marker not in prometheus.text
    assert marker not in history.text
    assert marker not in caplog.text


def test_prometheus_database_and_renderer_failures_are_fixed_and_fail_closed(
    client,
    monkeypatch,
    caplog,
) -> None:
    database_secret = "postgresql://user:password@database/private"
    original_collector = observability_api._collect_database_snapshot

    def fail_database(_settings):
        raise SQLAlchemyError(database_secret)

    monkeypatch.setattr(observability_api, "_collect_database_snapshot", fail_database)
    caplog.set_level("ERROR", logger="app.api.v1.observability")
    database_response = client.get("/api/v1/metrics/prometheus")

    assert database_response.status_code == 503
    assert database_response.json()["detail"]["code"] == "metrics_database_unavailable"
    assert database_secret not in database_response.text
    assert database_secret not in caplog.text

    monkeypatch.setattr(
        observability_api,
        "_collect_database_snapshot",
        original_collector,
    )

    def fail_renderer(*_args, **_kwargs):
        raise PrometheusRenderingError("sk-renderer-secret")

    monkeypatch.setattr(observability_api, "render_prometheus", fail_renderer)
    renderer_response = client.get("/api/v1/metrics/prometheus")

    assert renderer_response.status_code == 500
    assert renderer_response.json()["detail"]["code"] == "metrics_rendering_error"
    assert "sk-renderer-secret" not in renderer_response.text
    assert "sk-renderer-secret" not in caplog.text


def test_prometheus_collection_is_single_flight_per_api_process(client, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    original = observability_api._collect_database_snapshot

    def blocked(settings):
        entered.set()
        assert release.wait(timeout=2)
        return original(settings)

    monkeypatch.setattr(observability_api, "_collect_database_snapshot", blocked)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(client.get, "/api/v1/metrics/prometheus")
        assert entered.wait(timeout=1)
        second = client.get("/api/v1/metrics/prometheus")
        release.set()
        first = first_future.result(timeout=3)

    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "metrics_scrape_in_progress"
    assert first.status_code == 200


@pytest.mark.asyncio
async def test_prometheus_collection_gate_survives_repeated_request_cancellation(
    client,
    monkeypatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original = observability_api._collect_database_snapshot

    def blocked(settings):
        entered.set()
        assert release.wait(timeout=2)
        return original(settings)

    monkeypatch.setattr(observability_api, "_collect_database_snapshot", blocked)
    request = SimpleNamespace(
        url=SimpleNamespace(query=""),
        app=client.app,
    )
    first = asyncio.create_task(observability_api.prometheus_metrics(request, get_settings()))

    try:
        assert await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        assert observability_api._COLLECTION_LOCK.locked()
        with pytest.raises(HTTPException) as overlapping:
            await observability_api.prometheus_metrics(request, get_settings())
        assert overlapping.value.status_code == 429
        assert overlapping.value.detail["code"] == "metrics_scrape_in_progress"
    finally:
        release.set()
        for _ in range(100):
            if not observability_api._COLLECTION_LOCK.locked():
                break
            await asyncio.sleep(0.01)

    assert not observability_api._COLLECTION_LOCK.locked()


def test_prometheus_audit_cap_uses_limit_plus_one_and_never_truncates_counts(
    client,
    db_session,
) -> None:
    now = _database_now()
    for index in range(2):
        append_audit_event(
            db_session,
            event_key=f"metrics:cap:{index}",
            event_type="run_claimed",
            occurred_at=now - timedelta(seconds=index + 1),
            payload={"dispatch_count": index},
            run_id=f"metrics-cap-run-{index}",
            worker_id="metrics-cap-worker",
            attempt=1,
            lease_token=1,
        )
    db_session.commit()

    with SessionLocal() as session, session.begin():
        configure_read_snapshot(session)
        window_end = database_clock(session)
        with pytest.raises(MetricsObservationLimitExceeded):
            collect_task_history(
                session,
                window_start=window_end - timedelta(minutes=15),
                window_end=window_end,
                audit_event_limit=1,
            )
    assert METRICS_AUDIT_EVENT_LIMIT == 50_000


def test_prometheus_renderer_rejects_negative_or_nonfinite_snapshot_values(client) -> None:
    snapshot = observability_api._collect_database_snapshot(get_settings())

    with pytest.raises(PrometheusRenderingError):
        render_prometheus(
            replace(snapshot, current=replace(snapshot.current, pending=-1)),
            queue_configured=False,
            queue_available=False,
            recovery_alert_seconds=60,
        )
    with pytest.raises(PrometheusRenderingError):
        render_prometheus(
            snapshot,
            queue_configured=False,
            queue_available=False,
            recovery_alert_seconds=float("inf"),
        )
