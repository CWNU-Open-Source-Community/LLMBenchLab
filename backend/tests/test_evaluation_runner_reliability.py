"""Runner-level cancellation evidence for lease loss and process shutdown."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any

import pytest

from app.models import RunStatus
from app.runners.evaluation_runner import (
    EvaluationRunner,
    _ModelSnapshot,
    _QuestionSnapshot,
)
from app.runners.run_leases import AttemptDisposition, RunLease


class _FakeLeaseRepository:
    def __init__(self) -> None:
        self.lease = RunLease(
            run_id="run-controlled",
            owner="worker-controlled",
            token=1,
            attempt=1,
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
        )
        self.failed: list[str] = []
        self.finish_cancelled_calls = 0
        self.claim_calls = 0

    def claim(self, run_id: str, *, owner: str) -> RunLease | None:
        self.claim_calls += 1
        assert run_id == self.lease.run_id
        assert owner == self.lease.owner
        return self.lease

    def fail_attempt(self, lease: RunLease, *, error_code: str) -> AttemptDisposition:
        assert lease == self.lease
        self.failed.append(error_code)
        return AttemptDisposition.RETRY_SCHEDULED

    def finish_cancelled(self, lease: RunLease) -> bool:
        assert lease == self.lease
        self.finish_cancelled_calls += 1
        return False


class _ClosableAdapter:
    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class _ControlledRunner(EvaluationRunner):
    def __init__(self, *, concurrency: int, question_count: int = 2) -> None:
        self.repository = _FakeLeaseRepository()
        self._lease_repository = self.repository
        self._worker_id = self.repository.lease.owner
        self._heartbeat_seconds = 1
        self._session_factory = None
        self.concurrency = concurrency
        self.question_count = question_count
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.all_started = asyncio.Event()
        self.first_started = asyncio.Event()
        self.block = asyncio.Event()
        self.finished: list[RunStatus] = []

    def _load_snapshots(
        self, run_id: str
    ) -> tuple[
        _ModelSnapshot,
        list[_QuestionSnapshot],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]:
        assert run_id == self.repository.lease.run_id
        model = _ModelSnapshot(
            provider_type="mock",
            base_url=None,
            remote_model_name=None,
            api_key_env=None,
            input_price=None,
            output_price=None,
        )
        questions = [
            _QuestionSnapshot(
                id=f"question-{index}",
                external_id=f"q{index}",
                question_type="exact_match",
                prompt="One?",
                choices=None,
                reference_answer="one",
                evaluator_config={},
                metadata={},
            )
            for index in range(1, self.question_count + 1)
        ]
        return model, questions, {}, {}, {"concurrency": self.concurrency}

    def _cancellation_requested(self, run_id: str) -> bool:
        assert run_id == self.repository.lease.run_id
        return False

    def _finish(self, lease: RunLease, status: RunStatus) -> RunStatus | None:
        assert lease == self.repository.lease
        self.finished.append(status)
        return status

    async def _evaluate_question(
        self,
        lease: RunLease,
        model: _ModelSnapshot,
        question: _QuestionSnapshot,
        generation: dict[str, Any],
        prompt_template: dict[str, Any],
        adapter: Any,
    ) -> None:
        del lease, model, generation, prompt_template, adapter
        self.started.append(question.external_id)
        self.first_started.set()
        if len(self.started) == min(self.concurrency, self.question_count):
            self.all_started.set()
        try:
            await self.block.wait()
        except asyncio.CancelledError:
            self.cancelled.append(question.external_id)
            raise


class _LeaseLossRunner(_ControlledRunner):
    async def _heartbeat(
        self,
        lease: RunLease,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        del lease, stop
        await self.first_started.wait()
        lease_lost.set()


class _ShutdownRunner(_ControlledRunner):
    async def _heartbeat(
        self,
        lease: RunLease,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        del lease, lease_lost
        await stop.wait()


class _CancelledAtFinishRunner(_ControlledRunner):
    def _finish(self, lease: RunLease, status: RunStatus) -> RunStatus | None:
        assert lease == self.repository.lease
        self.finished.append(status)
        return RunStatus.CANCELLED


class _CancellationRunner(_ShutdownRunner):
    def __init__(self, *, concurrency: int, question_count: int) -> None:
        super().__init__(concurrency=concurrency, question_count=question_count)
        self.cancellation_checks = 0

    def _cancellation_requested(self, run_id: str) -> bool:
        assert run_id == self.repository.lease.run_id
        self.cancellation_checks += 1
        return self.cancellation_checks > 1


class _HeartbeatDuringLoadRunner(_ControlledRunner):
    def __init__(self) -> None:
        super().__init__(concurrency=1, question_count=1)
        self.load_started = Event()
        self.release_load = Event()
        self.heartbeat_started = asyncio.Event()

    def _load_snapshots(self, run_id: str):
        self.load_started.set()
        if not self.release_load.wait(timeout=2):
            raise RuntimeError("test_snapshot_load_timeout")
        return super()._load_snapshots(run_id)

    async def _heartbeat(
        self,
        lease: RunLease,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        del lease, lease_lost
        self.heartbeat_started.set()
        await stop.wait()


@pytest.mark.asyncio
async def test_known_lease_loss_cancels_inflight_and_does_not_start_waiting_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _ClosableAdapter()
    monkeypatch.setattr(
        "app.runners.evaluation_runner.build_adapter", lambda *args, **kwargs: adapter
    )
    runner = _LeaseLossRunner(concurrency=1)

    await runner.execute("run-controlled")

    assert runner.started == ["q1"]
    assert runner.cancelled == ["q1"]
    assert runner.finished == []
    assert runner.repository.failed == []
    assert runner.repository.finish_cancelled_calls == 1
    assert adapter.close_calls == 1


@pytest.mark.asyncio
async def test_process_shutdown_awaits_all_question_task_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _ClosableAdapter()
    monkeypatch.setattr(
        "app.runners.evaluation_runner.build_adapter", lambda *args, **kwargs: adapter
    )
    runner = _ShutdownRunner(concurrency=2)
    execution = asyncio.create_task(runner.execute("run-controlled"))
    await asyncio.wait_for(runner.all_started.wait(), timeout=2)

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert set(runner.started) == {"q1", "q2"}
    assert set(runner.cancelled) == {"q1", "q2"}
    assert runner.finished == []
    assert runner.repository.failed == []
    assert adapter.close_calls == 1


@pytest.mark.asyncio
async def test_graceful_shutdown_does_not_start_next_queued_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.runners.evaluation_runner.build_adapter", lambda *args, **kwargs: object()
    )
    runner = _ShutdownRunner(concurrency=1)
    shutdown_requested = asyncio.Event()
    execution = asyncio.create_task(
        runner.execute("run-controlled", shutdown_requested=shutdown_requested)
    )
    await asyncio.wait_for(runner.first_started.wait(), timeout=2)

    shutdown_requested.set()
    runner.block.set()
    await asyncio.wait_for(execution, timeout=2)

    assert runner.started == ["q1"]
    assert runner.cancelled == []
    assert runner.finished == []
    assert runner.repository.failed == []


@pytest.mark.asyncio
async def test_preexisting_shutdown_never_claims_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.runners.evaluation_runner.build_adapter", lambda *args, **kwargs: object()
    )
    runner = _ShutdownRunner(concurrency=1)
    shutdown_requested = asyncio.Event()
    shutdown_requested.set()

    await runner.execute("run-controlled", shutdown_requested=shutdown_requested)

    assert runner.started == []
    assert runner.repository.failed == []
    assert runner.repository.claim_calls == 0


@pytest.mark.asyncio
async def test_cancellation_stops_consumers_before_the_next_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.runners.evaluation_runner.build_adapter", lambda *args, **kwargs: object()
    )
    runner = _CancellationRunner(concurrency=1, question_count=1_000)
    runner.block.set()

    assert await runner.execute("run-controlled") is True

    assert runner.started == ["q1"]
    assert runner.finished == [RunStatus.CANCELLED]
    assert runner.repository.failed == []


@pytest.mark.asyncio
async def test_finish_log_uses_terminal_state_resolved_inside_database_lock(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "app.runners.evaluation_runner.build_adapter", lambda *args, **kwargs: object()
    )
    runner = _CancelledAtFinishRunner(concurrency=2)
    runner.block.set()
    caplog.set_level("INFO", logger="app.runners.evaluation_runner")

    assert await runner.execute("run-controlled") is True

    assert runner.finished == [RunStatus.COMPLETED]
    finish_record = next(
        record for record in caplog.records if record.event == "run_attempt_finished"
    )
    assert finish_record.result == "cancelled"


@pytest.mark.asyncio
async def test_large_question_set_uses_only_fixed_consumer_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.runners.evaluation_runner.build_adapter", lambda *args, **kwargs: object()
    )
    concurrency = 4
    question_count = 2_000
    runner = _ShutdownRunner(concurrency=concurrency, question_count=question_count)
    execution = asyncio.create_task(runner.execute("run-controlled"))
    await asyncio.wait_for(runner.all_started.wait(), timeout=2)

    question_tasks = {
        task.get_name(): task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("question-consumer")
        or task.get_name() == "question-lease-loss-watch"
    }

    assert set(question_tasks) == {
        "question-consumer-1",
        "question-consumer-2",
        "question-consumer-3",
        "question-consumer-4",
        "question-consumers",
        "question-lease-loss-watch",
    }
    assert runner.started == ["q1", "q2", "q3", "q4"]

    runner.block.set()
    assert await asyncio.wait_for(execution, timeout=2) is True

    assert len(runner.started) == question_count
    assert len(set(runner.started)) == question_count
    assert runner.cancelled == []
    assert runner.finished == [RunStatus.COMPLETED]


@pytest.mark.asyncio
async def test_heartbeat_runs_while_large_snapshot_is_materialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.runners.evaluation_runner.build_adapter", lambda *args, **kwargs: object()
    )
    runner = _HeartbeatDuringLoadRunner()
    runner.block.set()
    execution = asyncio.create_task(runner.execute("run-controlled"))

    assert await asyncio.to_thread(runner.load_started.wait, 2)
    await asyncio.wait_for(runner.heartbeat_started.wait(), timeout=1)
    assert not runner.release_load.is_set()

    runner.release_load.set()
    assert await asyncio.wait_for(execution, timeout=2) is True
    assert runner.started == ["q1"]
    assert runner.finished == [RunStatus.COMPLETED]
