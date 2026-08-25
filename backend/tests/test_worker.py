"""Independent Worker delivery, ACK, reconciliation, and shutdown tests."""

from __future__ import annotations

import asyncio
from collections import deque

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.runners.run_leases import ReapReport
from app.task_queue import QueueUnavailable, RunTaskDelivery
from app.workers import WorkerService


class _FakeRepository:
    def __init__(self, due: tuple[str, ...] = ()) -> None:
        self.due = due
        self.reap_calls = 0
        self.due_calls = 0
        self.fail_reap_once = False

    def reap_expired(self) -> ReapReport:
        self.reap_calls += 1
        if self.fail_reap_once:
            self.fail_reap_once = False
            raise SQLAlchemyError("sanitized test outage")
        return ReapReport()

    def due_run_ids(self, *, limit: int) -> tuple[str, ...]:
        assert limit == 1
        self.due_calls += 1
        return self.due[:1]


class _SequencedRepository(_FakeRepository):
    def __init__(self, due_sequences: list[tuple[str, ...]]) -> None:
        super().__init__()
        self._due_sequences = deque(due_sequences)

    def due_run_ids(self, *, limit: int) -> tuple[str, ...]:
        assert limit == 1
        self.due_calls += 1
        return self._due_sequences.popleft() if self._due_sequences else ()


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.raise_error = False
        self.before_return = None
        self.block = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        run_id: str,
        *,
        shutdown_requested: asyncio.Event | None = None,
    ) -> bool:
        self.calls.append(run_id)
        if self.before_return is not None:
            self.before_return()
        if self.raise_error:
            raise RuntimeError("controlled runner failure")
        if self.block:
            self.started.set()
            if shutdown_requested is None:
                await self.release.wait()
            else:
                await shutdown_requested.wait()
        return shutdown_requested is None or not shutdown_requested.is_set()


class _FakeQueue:
    def __init__(self, deliveries: list[RunTaskDelivery] | None = None) -> None:
        self.deliveries = deque(deliveries or [])
        self.acked: list[str] = []
        self.ensure_calls = 0
        self.claim_cursors: list[str] = []
        self.closed = False
        self.fail_ensure = False
        self.fail_ack = False
        self.read_started = asyncio.Event()
        self.release_read = asyncio.Event()
        self.block_read = False

    async def ensure_consumer_group(self) -> None:
        self.ensure_calls += 1
        if self.fail_ensure:
            raise QueueUnavailable("controlled queue outage")

    async def claim_stale(
        self,
        *,
        consumer: str,
        min_idle_milliseconds: int,
        start_id: str,
    ) -> tuple[str, RunTaskDelivery | None]:
        del consumer, min_idle_milliseconds
        self.claim_cursors.append(start_id)
        return "0-0", None

    async def read_new(
        self,
        *,
        consumer: str,
        block_milliseconds: int,
    ) -> RunTaskDelivery | None:
        del consumer, block_milliseconds
        self.read_started.set()
        if self.block_read:
            await self.release_read.wait()
        return self.deliveries.popleft() if self.deliveries else None

    async def ack(self, message_id: str) -> bool:
        if self.fail_ack:
            raise QueueUnavailable("controlled ack outage")
        self.acked.append(message_id)
        return True

    async def close(self) -> None:
        self.closed = True


def _settings(**updates):
    return get_settings().model_copy(
        update={
            "worker_poll_seconds": 0.01,
            "worker_shutdown_grace_seconds": 0.01,
            **updates,
        }
    )


def _service(repository, runner, queue) -> WorkerService:
    return WorkerService(
        SessionLocal,
        _settings(),
        run_queue=queue,
        worker_id="worker-test",
        lease_repository=repository,
        runner=runner,
    )


@pytest.mark.asyncio
async def test_worker_acks_only_after_runner_returns() -> None:
    delivery = RunTaskDelivery("1-0", "run-1", "correlation-1")
    queue = _FakeQueue([delivery])
    runner = _FakeRunner()

    def assert_not_acked() -> None:
        assert queue.acked == []

    runner.before_return = assert_not_acked
    service = _service(_FakeRepository(), runner, queue)

    assert await service.run_once() is True

    assert runner.calls == ["run-1"]
    assert queue.acked == ["1-0"]


@pytest.mark.asyncio
async def test_worker_does_not_ack_unhandled_runner_failure(caplog) -> None:
    queue = _FakeQueue([RunTaskDelivery("2-0", "run-2", "correlation-2")])
    runner = _FakeRunner()
    runner.raise_error = True
    service = _service(_FakeRepository(), runner, queue)
    caplog.set_level("ERROR", logger="app.workers.service")

    assert await service.run_once() is True

    assert runner.calls == ["run-2"]
    assert queue.acked == []
    failure = next(
        record for record in caplog.records if record.event == "worker_run_unhandled_error"
    )
    assert failure.run_id == "run-2"
    assert failure.correlation_id == "correlation-2"
    assert failure.result == "not_acknowledged"


@pytest.mark.asyncio
async def test_invalid_notification_is_acked_without_running_task() -> None:
    queue = _FakeQueue([RunTaskDelivery("3-0", None, None)])
    runner = _FakeRunner()
    service = _service(_FakeRepository(), runner, queue)

    assert await service.run_once() is True

    assert runner.calls == []
    assert queue.acked == ["3-0"]


@pytest.mark.asyncio
async def test_queue_outage_does_not_disable_database_reconciliation() -> None:
    queue = _FakeQueue()
    queue.fail_ensure = True
    repository = _SequencedRepository([(), ("run-database",)])
    runner = _FakeRunner()
    service = _service(repository, runner, queue)

    assert await service.run_once() is False
    assert await service.run_once() is True

    assert runner.calls == ["run-database"]


@pytest.mark.asyncio
async def test_temporary_database_failure_does_not_destroy_worker_service() -> None:
    repository = _FakeRepository(("run-after-recovery",))
    repository.fail_reap_once = True
    runner = _FakeRunner()
    service = _service(repository, runner, None)

    assert await service.run_once() is False
    assert await service.run_once() is True

    assert runner.calls == ["run-after-recovery"]


@pytest.mark.asyncio
async def test_stop_during_blocking_queue_read_never_starts_new_run() -> None:
    queue = _FakeQueue([RunTaskDelivery("4-0", "run-late", "correlation-4")])
    queue.block_read = True
    runner = _FakeRunner()
    service = _service(_FakeRepository(), runner, queue)
    stop = asyncio.Event()
    running = asyncio.create_task(service.run(stop))
    await asyncio.wait_for(queue.read_started.wait(), timeout=1)

    stop.set()
    await asyncio.wait_for(running, timeout=1)

    assert runner.calls == []
    assert queue.acked == []


@pytest.mark.asyncio
async def test_stop_during_database_scan_never_starts_new_run() -> None:
    stop = asyncio.Event()

    class _StoppingRepository(_FakeRepository):
        def due_run_ids(self, *, limit: int) -> tuple[str, ...]:
            assert limit == 1
            stop.set()
            return ("run-after-stop",)

    runner = _FakeRunner()
    service = _service(_StoppingRepository(), runner, None)

    await asyncio.wait_for(service.run(stop), timeout=1)

    assert runner.calls == []


@pytest.mark.asyncio
async def test_active_run_is_not_delayed_by_synchronous_reaping_on_shutdown() -> None:
    repository = _FakeRepository(("run-active",))
    runner = _FakeRunner()
    runner.block = True
    service = _service(repository, runner, None)
    stop = asyncio.Event()
    running = asyncio.create_task(service.run(stop))
    await asyncio.wait_for(runner.started.wait(), timeout=1)

    stop.set()
    await asyncio.wait_for(running, timeout=1)

    assert runner.calls == ["run-active"]
    assert repository.reap_calls == 1


@pytest.mark.asyncio
async def test_gracefully_drained_delivery_is_not_acked_before_lease_expiry_recovery() -> None:
    delivery = RunTaskDelivery("5-0", "run-draining", "correlation-5")
    queue = _FakeQueue([delivery])
    runner = _FakeRunner()
    runner.block = True
    service = _service(_FakeRepository(), runner, queue)
    stop = asyncio.Event()
    running = asyncio.create_task(service.run(stop))
    await asyncio.wait_for(runner.started.wait(), timeout=1)

    stop.set()
    await asyncio.wait_for(running, timeout=1)

    assert runner.calls == ["run-draining"]
    assert queue.acked == []
