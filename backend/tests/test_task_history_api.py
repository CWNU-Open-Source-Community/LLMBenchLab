"""Durable historical task counters and Run latency distributions."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.api.v1.health import _latency_summary
from app.governance.audit import append_audit_event
from app.models import (
    AuditRetentionClass,
    Benchmark,
    EvaluationRun,
    Model,
    ProviderType,
    RunStatus,
)


def _database_now(db_session) -> datetime:
    value = db_session.scalar(select(func.current_timestamp()))
    assert isinstance(value, datetime)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def test_run_created_at_default_is_database_expression() -> None:
    default = EvaluationRun.__table__.c.created_at.default
    assert default is not None
    assert getattr(default.arg, "name", None) == "current_timestamp"


def test_task_history_uses_typed_events_and_persisted_run_timestamps(
    client,
    db_session,
) -> None:
    now = _database_now(db_session)
    model = Model(id="history-model", name="History Mock", provider_type=ProviderType.MOCK)
    benchmark = Benchmark(
        id="history-benchmark",
        slug="history-benchmark",
        name="History benchmark",
        version="1.0.0",
        description="fixture",
        dimension="general",
        language="en",
        license="MIT",
        source="local",
        evaluator_type="exact_match",
        evaluator_config={},
        prompt_template={},
        dataset_hash="history-hash",
        question_count=1,
    )
    runs = [
        EvaluationRun(
            id="history-run-one",
            model_id=model.id,
            benchmark_id=benchmark.id,
            status=RunStatus.COMPLETED,
            model_parameters_snapshot={},
            benchmark_hash_snapshot=benchmark.dataset_hash,
            prompt_template_snapshot={},
            created_at=now - timedelta(hours=3),
            started_at=now - timedelta(hours=2, minutes=30),
            finished_at=now - timedelta(hours=1, minutes=30),
        ),
        EvaluationRun(
            id="history-run-two",
            model_id=model.id,
            benchmark_id=benchmark.id,
            status=RunStatus.FAILED,
            model_parameters_snapshot={},
            benchmark_hash_snapshot=benchmark.dataset_hash,
            prompt_template_snapshot={},
            created_at=now - timedelta(hours=2),
            started_at=now - timedelta(hours=1, minutes=45),
            finished_at=now - timedelta(minutes=45),
        ),
        EvaluationRun(
            id="history-run-pending",
            model_id=model.id,
            benchmark_id=benchmark.id,
            status=RunStatus.PENDING,
            model_parameters_snapshot={},
            benchmark_hash_snapshot=benchmark.dataset_hash,
            prompt_template_snapshot={},
            created_at=now - timedelta(hours=1),
        ),
        EvaluationRun(
            id="history-run-old",
            model_id=model.id,
            benchmark_id=benchmark.id,
            status=RunStatus.COMPLETED,
            model_parameters_snapshot={},
            benchmark_hash_snapshot=benchmark.dataset_hash,
            prompt_template_snapshot={},
            created_at=now - timedelta(hours=30),
            started_at=now - timedelta(hours=29, minutes=30),
            finished_at=now - timedelta(hours=28),
        ),
    ]
    db_session.add_all([model, benchmark, *runs])
    db_session.flush()

    for index, run_id in enumerate(("history-run-one", "history-run-two"), start=1):
        append_audit_event(
            db_session,
            event_key=f"history:{run_id}:claimed",
            event_type="run_claimed",
            occurred_at=now - timedelta(minutes=30 - index),
            payload={"dispatch_count": index},
            correlation_id=run_id,
            run_id=run_id,
            model_id=model.id,
            worker_id=f"worker-{index}",
            attempt=1,
            lease_token=1,
            duration_ms=999_999_999 if index == 1 else None,
        )
        append_audit_event(
            db_session,
            event_key=f"history:{run_id}:terminal",
            event_type="run_terminal",
            occurred_at=now - timedelta(minutes=20 - index),
            payload={
                "status": "completed" if index == 1 else "failed",
                "reason": "none" if index == 1 else "worker_error",
            },
            correlation_id=run_id,
            run_id=run_id,
            model_id=model.id,
        )
    append_audit_event(
        db_session,
        event_key="history:run-one:yielded",
        event_type="run_yielded",
        occurred_at=now - timedelta(minutes=15),
        payload={"responses_added": 2},
        correlation_id="history-run-one",
        run_id="history-run-one",
        model_id=model.id,
        worker_id="worker-1",
        attempt=1,
        lease_token=1,
    )
    append_audit_event(
        db_session,
        event_key="history:run-one:queue",
        event_type="queue_notification",
        occurred_at=now - timedelta(minutes=10),
        payload={"result": "published"},
        correlation_id="history-run-one",
        run_id="history-run-one",
        model_id=model.id,
    )
    append_audit_event(
        db_session,
        event_key="history:policy:applied",
        event_type="governance_policy_applied",
        occurred_at=now - timedelta(minutes=5),
        payload={"policy_version": 2, "policy_hash": "a" * 64},
    )
    append_audit_event(
        db_session,
        event_key="history:run-two:dead-lettered",
        event_type="run_dead_lettered",
        occurred_at=now - timedelta(minutes=4),
        payload={"failed_attempt_count": 3, "reason": "worker_error"},
        correlation_id="history-run-two",
        run_id="history-run-two",
        model_id=model.id,
    )
    append_audit_event(
        db_session,
        event_key="history:credential:changed",
        event_type="credential_changed",
        occurred_at=now - timedelta(minutes=3),
        payload={
            "action": "created",
            "credential_source": "stored",
            "key_id": "fixture-v1",
        },
        retention_class=AuditRetentionClass.SECURITY,
        model_id=model.id,
    )
    append_audit_event(
        db_session,
        event_key="history:old:claimed",
        event_type="run_claimed",
        occurred_at=now - timedelta(hours=25),
        payload={"dispatch_count": 1},
        correlation_id="history-run-old",
        run_id="history-run-old",
        model_id=model.id,
        worker_id="worker-old",
        attempt=1,
        lease_token=1,
    )
    db_session.commit()

    response = client.get("/api/v1/tasks/history?window_hours=24")

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_hours"] == 24
    assert payload["latency_sample_limit"] == 10_000
    assert payload["timestamp"] == payload["window_end"]
    assert datetime.fromisoformat(payload["window_end"]) - datetime.fromisoformat(
        payload["window_start"]
    ) == timedelta(hours=24)
    assert payload["event_counts"] == {
        "total": 8,
        "governance_policy_bootstrapped": 0,
        "governance_policy_applied": 1,
        "run_admitted": 0,
        "run_claimed": 2,
        "run_cancel_requested": 0,
        "run_deferred": 0,
        "run_yielded": 1,
        "run_terminal": 2,
        "run_retry_scheduled": 0,
        "run_dead_lettered": 1,
        "run_lease_reconciled": 0,
        "provider_attempt_reserved": 0,
        "provider_attempt_send_started": 0,
        "provider_attempt_settled": 0,
        "question_evidence_persisted": 0,
        "queue_notification": 1,
        "governance_integrity_error": 0,
    }
    assert payload["queue_latency"] == {
        "sample_count": 2,
        "truncated": False,
        "p50_ms": 1_350_000.0,
        "p95_ms": 1_755_000.0,
        "p99_ms": 1_791_000.0,
    }
    assert payload["execution_latency"] == {
        "sample_count": 2,
        "truncated": False,
        "p50_ms": 3_600_000.0,
        "p95_ms": 3_600_000.0,
        "p99_ms": 3_600_000.0,
    }
    assert payload["end_to_end_latency"] == {
        "sample_count": 2,
        "truncated": False,
        "p50_ms": 4_950_000.0,
        "p95_ms": 5_355_000.0,
        "p99_ms": 5_391_000.0,
    }


def test_task_history_empty_window_and_bounds_are_explicit(client) -> None:
    empty = client.get("/api/v1/tasks/history?window_hours=1")

    assert empty.status_code == 200
    assert empty.json()["event_counts"]["total"] == 0
    for name in ("queue_latency", "execution_latency", "end_to_end_latency"):
        assert empty.json()[name] == {
            "sample_count": 0,
            "truncated": False,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
        }
    assert client.get("/api/v1/tasks/history?window_hours=0").status_code == 422
    assert client.get("/api/v1/tasks/history?window_hours=2161").status_code == 422


@pytest.mark.parametrize("corrupt_field", ["payload", "payload_hash"])
def test_task_history_fails_closed_without_counting_or_reflecting_corrupt_audit(
    client,
    db_session,
    caplog,
    corrupt_field: str,
) -> None:
    marker = "sk-history-integrity-marker-never-reflect"
    event = append_audit_event(
        db_session,
        event_key=f"history:corrupt:{corrupt_field}",
        event_type="run_claimed",
        occurred_at=_database_now(db_session) - timedelta(minutes=1),
        payload={"dispatch_count": 1},
        correlation_id="history-corrupt-run",
        run_id="history-corrupt-run",
        worker_id="history-corrupt-worker",
        attempt=1,
        lease_token=1,
    )
    if corrupt_field == "payload":
        event.payload["dispatch_count"] = marker
    else:
        event.payload_hash = marker + "x" * (64 - len(marker))
    db_session.commit()
    caplog.set_level("ERROR", logger="app.api.v1.health")

    response = client.get("/api/v1/tasks/history?window_hours=24")

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "audit_event_integrity_error",
            "message": "A retained audit event failed integrity validation",
        }
    }
    assert marker not in response.text
    assert marker not in caplog.text
    assert any(
        getattr(record, "event", None) == "task_history_audit_integrity_error"
        and getattr(record, "result", None) == "rejected"
        for record in caplog.records
    )


def test_latency_sample_cap_is_deterministic_and_disclosed() -> None:
    summary = _latency_summary([float(value) for value in range(10_001)])

    assert summary.sample_count == 10_000
    assert summary.truncated is True
    assert summary.p50_ms == 4_999.5
    assert summary.p95_ms == pytest.approx(9_499.05)
    assert summary.p99_ms == pytest.approx(9_899.01)
