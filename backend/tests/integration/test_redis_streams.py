"""Real Redis Streams PEL and XAUTOCLAIM evidence; skipped without an explicit URL."""

from __future__ import annotations

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import EvaluationResponse, EvaluationRun
from app.runners.run_leases import RunLeaseRepository
from app.task_queue import QueueUnavailable, RedisRunQueue
from app.workers import WorkerService

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_real_redis_pending_delivery_is_autoclaimed_and_acked() -> None:
    redis_url = os.environ.get("LLMBENCHLAB_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("LLMBENCHLAB_TEST_REDIS_URL is required")
    suffix = uuid4().hex
    stream = f"llmbenchlab:test:runs:{suffix}"
    group = f"llmbenchlab-test-workers-{suffix}"
    raw = Redis.from_url(redis_url, decode_responses=True)
    queue = RedisRunQueue.from_url(
        redis_url,
        stream=stream,
        consumer_group=group,
        max_length=100,
        block_milliseconds=100,
        max_connections=4,
        publish_timeout_seconds=1,
        operation_timeout_seconds=1,
    )
    try:
        server = await raw.info("server")
        version = tuple(int(part) for part in server["redis_version"].split(".")[:2])
        assert version >= (6, 2)

        run_id = str(uuid4())
        correlation_id = str(uuid4())
        await queue.publish(run_id, correlation_id=correlation_id)
        await queue.ensure_consumer_group()
        first = await queue.read_new(consumer="worker-a", block_milliseconds=100)
        assert first is not None and first.run_id == run_id

        cursor, claimed = await queue.claim_stale(
            consumer="worker-b",
            min_idle_milliseconds=0,
            start_id="0-0",
        )
        assert cursor
        assert claimed == first
        assert await queue.ack(claimed.message_id) is True

        _cursor, empty = await queue.claim_stale(
            consumer="worker-c",
            min_idle_milliseconds=0,
            start_id="0-0",
        )
        assert empty is None
        assert await raw.xpending(stream, group) == {
            "pending": 0,
            "min": None,
            "max": None,
            "consumers": [],
        }
    finally:
        await raw.delete(stream)
        await queue.close()
        await raw.aclose()


class _QueueFirstRepository(RunLeaseRepository):
    def due_run_ids(self, *, limit: int) -> tuple[str, ...]:
        assert limit == 1
        return ()


def _run_snapshot(run_id: str) -> dict[str, object]:
    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        responses = list(
            session.scalars(
                select(EvaluationResponse)
                .where(EvaluationResponse.run_id == run_id)
                .order_by(EvaluationResponse.id)
            )
        )
        assert run is not None
        return {
            "status": run.status.value,
            "protocol_version": run.protocol_version,
            "attempt_count": run.attempt_count,
            "score": run.score,
            "completion_rate": run.completion_rate,
            "answered_accuracy": run.answered_accuracy,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "estimated_cost": run.estimated_cost,
            "response_ids": tuple(response.id for response in responses),
            "response_scores": tuple(response.score for response in responses),
        }


class _AckResultUnknownQueue:
    def __init__(self, queue: RedisRunQueue, run_id: str) -> None:
        self._queue = queue
        self._run_id = run_id
        self.fail_after_first_ack = True
        self.snapshot_before_ack: dict[str, object] | None = None

    async def ensure_consumer_group(self) -> None:
        await self._queue.ensure_consumer_group()

    async def publish(self, run_id: str, *, correlation_id: str) -> str:
        return await self._queue.publish(run_id, correlation_id=correlation_id)

    async def claim_stale(self, **kwargs):
        return await self._queue.claim_stale(**kwargs)

    async def read_new(self, **kwargs):
        return await self._queue.read_new(**kwargs)

    async def ack(self, message_id: str) -> bool:
        self.snapshot_before_ack = _run_snapshot(self._run_id)
        assert self.snapshot_before_ack["status"] == "completed"
        assert len(self.snapshot_before_ack["response_ids"]) == 15
        acknowledged = await self._queue.ack(message_id)
        if self.fail_after_first_ack:
            self.fail_after_first_ack = False
            raise QueueUnavailable("ack_result_unknown")
        return acknowledged

    async def close(self) -> None:
        await self._queue.close()


@pytest.mark.asyncio
async def test_real_worker_commits_protocol_evidence_before_ack_and_duplicate_is_noop(
    client,
) -> None:
    redis_url = os.environ.get("LLMBENCHLAB_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("LLMBENCHLAB_TEST_REDIS_URL is required")
    suffix = uuid4().hex
    stream = f"llmbenchlab:test:worker:{suffix}"
    group = f"llmbenchlab-test-worker-{suffix}"
    raw = Redis.from_url(redis_url, decode_responses=True)
    queue = RedisRunQueue.from_url(
        redis_url,
        stream=stream,
        consumer_group=group,
        max_length=100,
        block_milliseconds=100,
        max_connections=4,
        publish_timeout_seconds=1,
        operation_timeout_seconds=1,
    )
    model = client.post(
        "/api/v1/models",
        json={"name": "Redis Integration Mock", "provider_type": "mock", "enabled": True},
    )
    benchmark = client.post("/api/v1/benchmarks/reload-demo")
    assert model.status_code == 201
    assert benchmark.status_code == 200
    created = client.post(
        "/api/v1/runs",
        json={
            "model_id": model.json()["id"],
            "benchmark_id": benchmark.json()["id"],
            "concurrency": 2,
        },
    )
    assert created.status_code == 202
    run_id = created.json()["id"]
    await queue.publish(run_id, correlation_id=str(uuid4()))
    settings = get_settings()
    repository = _QueueFirstRepository(
        SessionLocal,
        lease_for=timedelta(seconds=settings.worker_lease_seconds),
    )
    ambiguous_queue = _AckResultUnknownQueue(queue, run_id)
    worker = WorkerService(
        SessionLocal,
        settings,
        run_queue=ambiguous_queue,  # type: ignore[arg-type]
        worker_id="worker-real-redis",
        lease_repository=repository,
    )
    try:
        assert await worker.run_once() is True
        durable = ambiguous_queue.snapshot_before_ack
        assert durable is not None
        assert durable["protocol_version"] == "llmbenchlab-protocol-v1"
        assert durable["attempt_count"] == 1
        assert durable["score"] == 100
        assert durable["completion_rate"] == 100
        assert durable["answered_accuracy"] == 100
        assert durable["input_tokens"] == 120
        assert durable["output_tokens"] == 30
        assert float(durable["estimated_cost"] or 0) == 0
        assert (await raw.xpending(stream, group))["pending"] == 0

        await ambiguous_queue.publish(run_id, correlation_id=str(uuid4()))
        assert await worker.run_once() is True

        assert _run_snapshot(run_id) == durable
        assert (await raw.xpending(stream, group))["pending"] == 0
    finally:
        await raw.delete(stream)
        await worker.close()
        await raw.aclose()
