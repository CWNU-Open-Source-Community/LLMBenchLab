"""API dispatch ordering and queue-degradation tests without an in-process Runner."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import create_app
from app.models import EvaluationResponse, EvaluationRun, RunStatus
from app.runners.run_leases import RunLeaseRepository
from app.task_queue import QueueUnavailable, RedisRunQueue, RunTaskDelivery
from app.workers import WorkerService


def _create_inputs(client) -> tuple[str, str]:
    model = client.post(
        "/api/v1/models",
        json={"name": "Dispatch Mock", "provider_type": "mock", "enabled": True},
    )
    assert model.status_code == 201
    benchmark = client.post("/api/v1/benchmarks/reload-demo")
    assert benchmark.status_code == 200
    return model.json()["id"], benchmark.json()["id"]


def _post_run(client, model_id: str, benchmark_id: str):
    return client.post(
        "/api/v1/runs",
        json={"model_id": model_id, "benchmark_id": benchmark_id, "concurrency": 2},
    )


class _InspectingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.committed_before_publish = False

    async def publish(self, run_id: str, *, correlation_id: str) -> str:
        with SessionLocal() as independent_session:
            run = independent_session.get(EvaluationRun, run_id)
            response_count = independent_session.scalar(
                select(func.count(EvaluationResponse.id)).where(EvaluationResponse.run_id == run_id)
            )
            self.committed_before_publish = (
                run is not None
                and run.status == RunStatus.PENDING
                and run.attempt_count == 0
                and response_count == 0
            )
        self.calls.append((run_id, correlation_id))
        return "1-0"


class _FailingPublisher:
    def __init__(self) -> None:
        self.calls = 0

    async def publish(self, run_id: str, *, correlation_id: str) -> str:
        del run_id, correlation_id
        self.calls += 1
        raise QueueUnavailable("sanitized_test_failure")


class _BlockingRedis:
    async def xadd(self, *args, **kwargs):
        del args, kwargs
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        return None


class _CompletingPublisher:
    async def publish(self, run_id: str, *, correlation_id: str) -> str:
        assert correlation_id
        worker = WorkerService(
            SessionLocal,
            get_settings(),
            run_queue=None,
            worker_id="worker-publish-race",
        )
        assert await worker.run_once() is True
        with SessionLocal() as session:
            run = session.get(EvaluationRun, run_id)
            assert run is not None and run.status == RunStatus.COMPLETED
        return "5-0"


class _AmbiguousQueue:
    def __init__(self) -> None:
        self.delivery: RunTaskDelivery | None = None
        self.acked: list[str] = []

    async def publish(self, run_id: str, *, correlation_id: str) -> str:
        self.delivery = RunTaskDelivery("6-0", run_id, correlation_id)
        raise QueueUnavailable("xadd_result_unknown")

    async def ensure_consumer_group(self) -> None:
        return None

    async def claim_stale(
        self,
        *,
        consumer: str,
        min_idle_milliseconds: int,
        start_id: str,
    ) -> tuple[str, RunTaskDelivery | None]:
        del consumer, min_idle_milliseconds, start_id
        return "0-0", None

    async def read_new(
        self,
        *,
        consumer: str,
        block_milliseconds: int,
    ) -> RunTaskDelivery | None:
        del consumer, block_milliseconds
        delivery, self.delivery = self.delivery, None
        return delivery

    async def ack(self, message_id: str) -> bool:
        self.acked.append(message_id)
        return True

    async def close(self) -> None:
        return None


def test_create_commits_before_publish_and_api_never_executes_run(
    client,
    monkeypatch,
) -> None:
    publisher = _InspectingPublisher()
    monkeypatch.setattr(client.app.state, "run_queue", publisher)
    model_id, benchmark_id = _create_inputs(client)

    created = _post_run(client, model_id, benchmark_id)

    assert created.status_code == 202
    payload = created.json()
    assert publisher.committed_before_publish is True
    assert len(publisher.calls) == 1
    assert publisher.calls[0][0] == payload["id"]
    assert publisher.calls[0][1]
    assert payload["status"] == "pending"
    assert payload["attempt_count"] == 0
    assert payload["lease_owner"] is None
    assert payload["last_enqueued_at"] is not None
    assert client.get(f"/api/v1/runs/{payload['id']}/responses").json()["total"] == 0
    assert not hasattr(client.app.state, "task_manager")


def test_queue_publish_failure_still_returns_recoverable_pending_run(
    client,
    monkeypatch,
) -> None:
    publisher = _FailingPublisher()
    monkeypatch.setattr(client.app.state, "run_queue", publisher)
    model_id, benchmark_id = _create_inputs(client)

    created = _post_run(client, model_id, benchmark_id)

    assert created.status_code == 202
    payload = created.json()
    assert publisher.calls == 1
    assert payload["status"] == "pending"
    assert payload["attempt_count"] == 0
    assert payload["lease_owner"] is None
    assert payload["last_enqueued_at"] is None
    assert payload["last_error"] == "queue_notification_unavailable"
    assert client.get(f"/api/v1/runs/{payload['id']}/responses").json()["total"] == 0


def test_database_commit_failure_never_calls_publisher(client, monkeypatch) -> None:
    publisher = _InspectingPublisher()
    monkeypatch.setattr(client.app.state, "run_queue", publisher)
    model_id, benchmark_id = _create_inputs(client)

    def fail_commit(_session: Session) -> None:
        raise SQLAlchemyError("controlled commit failure")

    monkeypatch.setattr(Session, "commit", fail_commit)
    response = _post_run(client, model_id, benchmark_id)

    assert response.status_code == 500
    assert response.headers["X-Request-ID"]
    assert response.json()["detail"]["code"] == "internal_server_error"
    assert "controlled commit failure" not in response.text
    assert publisher.calls == []
    with SessionLocal() as session:
        assert session.scalar(select(func.count(EvaluationRun.id))) == 0


def test_endpoint_bounds_half_open_redis_publish_and_returns_202(client, monkeypatch) -> None:
    queue = RedisRunQueue(
        _BlockingRedis(),  # type: ignore[arg-type]
        stream="runs",
        consumer_group="workers",
        max_length=100,
        default_block_milliseconds=100,
        publish_timeout_seconds=0.01,
        operation_timeout_seconds=0.01,
    )
    monkeypatch.setattr(client.app.state, "run_queue", queue)
    model_id, benchmark_id = _create_inputs(client)

    started = time.monotonic()
    created = _post_run(client, model_id, benchmark_id)
    elapsed = time.monotonic() - started

    assert created.status_code == 202
    assert elapsed < 0.5
    assert created.json()["status"] == "pending"
    assert created.json()["attempt_count"] == 0
    assert created.json()["last_error"] == "queue_notification_unavailable"


def test_publish_audit_cannot_overwrite_worker_terminal_state(client, monkeypatch) -> None:
    monkeypatch.setattr(client.app.state, "run_queue", _CompletingPublisher())
    model_id, benchmark_id = _create_inputs(client)

    created = _post_run(client, model_id, benchmark_id)

    assert created.status_code == 202
    payload = created.json()
    assert payload["status"] == "completed"
    assert payload["attempt_count"] == 1
    assert payload["completed_questions"] == payload["total_questions"] == 15
    assert payload["score"] == 100
    assert payload["last_enqueued_at"] is None
    assert payload["last_error"] is None


def test_ambiguous_publish_and_database_reconciliation_remain_idempotent(
    client,
    monkeypatch,
) -> None:
    queue = _AmbiguousQueue()
    monkeypatch.setattr(client.app.state, "run_queue", queue)
    model_id, benchmark_id = _create_inputs(client)
    created = _post_run(client, model_id, benchmark_id)
    assert created.status_code == 202
    run_id = created.json()["id"]
    assert created.json()["last_error"] == "queue_notification_unavailable"
    worker = WorkerService(
        SessionLocal,
        get_settings(),
        run_queue=queue,  # type: ignore[arg-type]
        worker_id="worker-ambiguous-publish",
    )

    assert asyncio.run(worker.run_once()) is True
    assert queue.acked == []
    assert asyncio.run(worker.run_once()) is True

    assert queue.acked == ["6-0"]
    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        response_count = session.scalar(
            select(func.count(EvaluationResponse.id)).where(EvaluationResponse.run_id == run_id)
        )
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        assert run.attempt_count == 1
        assert run.completed_questions == response_count == 15


def test_api_restart_does_not_mutate_running_lease(client) -> None:
    model_id, benchmark_id = _create_inputs(client)
    created = _post_run(client, model_id, benchmark_id)
    run_id = created.json()["id"]
    settings = get_settings()
    repository = RunLeaseRepository(
        SessionLocal,
        lease_for=timedelta(seconds=settings.worker_lease_seconds),
    )
    lease = repository.claim(run_id, owner="worker-restart-proof")
    assert lease is not None
    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        before = (
            run.status,
            run.attempt_count,
            run.lease_owner,
            run.lease_token,
            run.lease_expires_at,
            run.heartbeat_at,
        )

    for _ in range(2):
        with TestClient(create_app()) as restarted_api:
            assert restarted_api.get("/api/v1/health").status_code == 200

    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        after = (
            run.status,
            run.attempt_count,
            run.lease_owner,
            run.lease_token,
            run.lease_expires_at,
            run.heartbeat_at,
        )
    assert after == before
