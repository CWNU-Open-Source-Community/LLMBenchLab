"""Runner-level cancellation evidence for lease loss and process shutdown."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.adapters import ModelGenerationResult, ProviderAttemptContext
from app.governance import (
    GovernanceDeferred,
    GovernanceExhausted,
    GovernanceFenceLost,
    GovernanceIntegrityError,
    GovernanceSettlementUnknown,
)
from app.models import RunStatus
from app.runners.evaluation_runner import (
    EvaluationRunner,
    _GovernanceSnapshot,
    _ModelSnapshot,
    _QuestionSnapshot,
)
from app.runners.run_leases import (
    AttemptDisposition,
    ResponseDisposition,
    RunLease,
)


def _assert_outside_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AssertionError("synchronous repository call ran on the event-loop thread")


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
        self.deferred: list[tuple[str, datetime]] = []
        self.exhausted: list[tuple[str, bool]] = []
        self.yielded: list[int] = []
        self.responses: list[Any] = []

    def claim(self, run_id: str, *, owner: str) -> RunLease | None:
        _assert_outside_event_loop()
        self.claim_calls += 1
        assert run_id == self.lease.run_id
        assert owner == self.lease.owner
        return self.lease

    def fail_attempt(self, lease: RunLease, *, error_code: str) -> AttemptDisposition:
        _assert_outside_event_loop()
        assert lease == self.lease
        self.failed.append(error_code)
        return AttemptDisposition.RETRY_SCHEDULED

    def finish_cancelled(self, lease: RunLease) -> bool:
        _assert_outside_event_loop()
        assert lease == self.lease
        self.finish_cancelled_calls += 1
        return False

    def defer_governance(
        self,
        lease: RunLease,
        *,
        reason: str,
        not_before: datetime,
    ) -> AttemptDisposition:
        _assert_outside_event_loop()
        assert lease == self.lease
        self.deferred.append((reason, not_before))
        return AttemptDisposition.GOVERNANCE_DEFERRED

    def exhaust_governance(
        self,
        lease: RunLease,
        *,
        reason: str,
        integrity_error: bool = False,
    ) -> AttemptDisposition:
        _assert_outside_event_loop()
        assert lease == self.lease
        self.exhausted.append((reason, integrity_error))
        return AttemptDisposition.GOVERNANCE_EXHAUSTED

    def cooperative_yield(
        self,
        lease: RunLease,
        *,
        responses_added: int,
    ) -> AttemptDisposition:
        _assert_outside_event_loop()
        assert lease == self.lease
        self.yielded.append(responses_added)
        return AttemptDisposition.COOPERATIVE_YIELD

    def persist_response(self, lease: RunLease, response: Any) -> ResponseDisposition:
        _assert_outside_event_loop()
        assert lease == self.lease
        self.responses.append(response)
        return ResponseDisposition.INSERTED


class _ClosableAdapter:
    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class _ControlledRunner(EvaluationRunner):
    def __init__(
        self,
        *,
        concurrency: int,
        question_count: int = 2,
        governance: _GovernanceSnapshot | None = None,
    ) -> None:
        self.repository = _FakeLeaseRepository()
        self._lease_repository = self.repository
        self._worker_id = self.repository.lease.owner
        self._heartbeat_seconds = 1
        self._session_factory = None
        self._governance_repository = object()
        self.concurrency = concurrency
        self.question_count = question_count
        self.governance = governance
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
            governance=self.governance,
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
        _assert_outside_event_loop()
        assert run_id == self.repository.lease.run_id
        return False

    def _finish(self, lease: RunLease, status: RunStatus) -> RunStatus | None:
        _assert_outside_event_loop()
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
    ) -> ResponseDisposition:
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
        return ResponseDisposition.INSERTED


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
        _assert_outside_event_loop()
        assert lease == self.repository.lease
        self.finished.append(status)
        return RunStatus.CANCELLED


class _CancellationRunner(_ShutdownRunner):
    def __init__(self, *, concurrency: int, question_count: int) -> None:
        super().__init__(concurrency=concurrency, question_count=question_count)
        self.cancellation_checks = 0

    def _cancellation_requested(self, run_id: str) -> bool:
        _assert_outside_event_loop()
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


def _managed_governance(*, question_quantum: int = 25) -> _GovernanceSnapshot:
    return _GovernanceSnapshot(
        model_id="model-managed",
        provider_scope="a" * 64,
        question_quantum=question_quantum,
        input_token_reservation=100,
        lifetime_request_budget=None,
        lifetime_token_budget=None,
        lifetime_cost_budget_usd=None,
    )


class _ManagedSignalRunner(_ShutdownRunner):
    def __init__(self, signal: BaseException) -> None:
        super().__init__(
            concurrency=1,
            question_count=1,
            governance=_managed_governance(),
        )
        self.signal = signal

    async def _evaluate_question(
        self,
        lease: RunLease,
        model: _ModelSnapshot,
        question: _QuestionSnapshot,
        generation: dict[str, Any],
        prompt_template: dict[str, Any],
        adapter: Any,
    ) -> ResponseDisposition:
        del lease, model, generation, prompt_template, adapter
        self.started.append(question.external_id)
        raise self.signal


class _RecordingResponseRepository:
    def __init__(self) -> None:
        self.responses: list[Any] = []

    def persist_response(self, lease: RunLease, response: Any) -> ResponseDisposition:
        _assert_outside_event_loop()
        del lease
        self.responses.append(response)
        return ResponseDisposition.INSERTED


class _BlockingHeartbeatRepository:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()

    def heartbeat(self, lease: RunLease) -> RunLease:
        _assert_outside_event_loop()
        self.entered.set()
        if not self.release.wait(timeout=1):
            raise AssertionError("event_loop_was_blocked")
        return lease


class _RecordingGovernanceRepository:
    def __init__(self, context: ProviderAttemptContext) -> None:
        self.context = context
        self.question_context_calls: list[dict[str, Any]] = []

    def question_context(self, **kwargs: Any) -> ProviderAttemptContext:
        _assert_outside_event_loop()
        self.question_context_calls.append(kwargs)
        return self.context


class _FailingGovernanceRepository:
    def question_context(self, **kwargs: Any) -> ProviderAttemptContext:
        _assert_outside_event_loop()
        del kwargs
        raise SQLAlchemyError("question context acknowledgement unavailable")


class _ManagedQuestionRunner(_ShutdownRunner):
    async def _evaluate_question(
        self,
        lease: RunLease,
        model: _ModelSnapshot,
        question: _QuestionSnapshot,
        generation: dict[str, Any],
        prompt_template: dict[str, Any],
        adapter: Any,
    ) -> ResponseDisposition:
        return await EvaluationRunner._evaluate_question(
            self,
            lease,
            model,
            question,
            generation,
            prompt_template,
            adapter,
        )


class _ResultAdapter:
    def __init__(
        self,
        *,
        result: ModelGenerationResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[list[dict[str, str]], dict[str, Any], dict[str, Any]]] = []

    async def generate(
        self,
        messages: list[dict[str, str]],
        config: dict[str, Any],
        **kwargs: Any,
    ) -> ModelGenerationResult:
        self.calls.append((messages, config, kwargs))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _question() -> _QuestionSnapshot:
    return _QuestionSnapshot(
        id="question-managed",
        external_id="q-managed",
        question_type="exact_match",
        prompt="One?",
        choices=None,
        reference_answer="one",
        evaluator_config={},
        metadata={},
    )


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
    assert set(runner.started) == {"q1", "q2", "q3", "q4"}

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


@pytest.mark.asyncio
async def test_blocking_heartbeat_repository_call_does_not_block_event_loop() -> None:
    repository = _BlockingHeartbeatRepository()
    runner = object.__new__(EvaluationRunner)
    runner._lease_repository = repository
    runner._heartbeat_seconds = 0.001
    lease = _FakeLeaseRepository().lease
    stop = asyncio.Event()
    lease_lost = asyncio.Event()

    async def release_from_event_loop() -> None:
        while not repository.entered.is_set():
            await asyncio.sleep(0)
        repository.release.set()
        stop.set()

    heartbeat = asyncio.create_task(runner._heartbeat(lease, stop, lease_lost))
    ticker = asyncio.create_task(release_from_event_loop())

    await asyncio.wait_for(asyncio.gather(heartbeat, ticker), timeout=2)

    assert repository.entered.is_set()
    assert not lease_lost.is_set()


@pytest.mark.asyncio
async def test_governance_defer_stops_new_questions_but_drains_inflight_question() -> None:
    questions = [_question() for _ in range(3)]
    questions[0] = replace(questions[0], id="question-1", external_id="q1")
    questions[1] = replace(questions[1], id="question-2", external_id="q2")
    questions[2] = replace(questions[2], id="question-3", external_id="q3")
    started: list[str] = []
    completed: list[str] = []
    both_started = asyncio.Event()
    release_second = asyncio.Event()
    not_before = datetime.now(UTC) + timedelta(minutes=1)

    async def evaluate(question: _QuestionSnapshot) -> bool:
        started.append(question.external_id)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        if question.external_id == "q1":
            raise GovernanceDeferred("governance_global_rpm", not_before=not_before)
        await release_second.wait()
        completed.append(question.external_id)
        return True

    execution = asyncio.create_task(
        EvaluationRunner._run_questions(
            questions,
            evaluate=evaluate,
            concurrency=2,
            lease_lost=asyncio.Event(),
        )
    )
    await asyncio.wait_for(both_started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert not execution.done()
    assert started == ["q1", "q2"]

    release_second.set()
    signal = await asyncio.wait_for(execution, timeout=1)

    assert isinstance(signal, GovernanceDeferred)
    assert signal.not_before == not_before
    assert completed == ["q2"]
    assert "q3" not in started


@pytest.mark.asyncio
async def test_managed_run_yields_after_frozen_question_quantum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_options: list[dict[str, Any]] = []

    def build(*args: Any, **kwargs: Any) -> object:
        del args
        adapter_options.append(kwargs)
        return object()

    monkeypatch.setattr("app.runners.evaluation_runner.build_adapter", build)
    runner = _ShutdownRunner(
        concurrency=4,
        question_count=5,
        governance=_managed_governance(question_quantum=2),
    )
    runner.block.set()

    assert await runner.execute("run-controlled") is True

    assert set(runner.started) == {"q1", "q2"}
    assert runner.repository.yielded == [2]
    assert runner.repository.failed == []
    assert runner.finished == []
    assert "attempt_controller" in adapter_options[0]


@pytest.mark.parametrize(
    ("signal", "expected_transition"),
    [
        (
            GovernanceDeferred(
                "governance_provider_rpm",
                not_before=datetime(2026, 8, 27, 15, 1, tzinfo=UTC),
            ),
            "deferred",
        ),
        (GovernanceExhausted("governance_run_budget"), "exhausted"),
        (GovernanceIntegrityError("governance_ledger_integrity"), "integrity"),
        (GovernanceSettlementUnknown(), "yielded"),
        (GovernanceFenceLost("governance_lease_fence_lost"), "fenced"),
    ],
)
@pytest.mark.asyncio
async def test_governance_signals_never_consume_worker_failure_budget(
    monkeypatch: pytest.MonkeyPatch,
    signal: BaseException,
    expected_transition: str,
) -> None:
    monkeypatch.setattr(
        "app.runners.evaluation_runner.build_adapter", lambda *args, **kwargs: object()
    )
    runner = _ManagedSignalRunner(signal)

    assert await runner.execute("run-controlled") is True

    assert runner.repository.failed == []
    assert runner.finished == []
    if expected_transition == "deferred":
        assert isinstance(signal, GovernanceDeferred)
        assert runner.repository.deferred == [(signal.code, signal.not_before)]
    elif expected_transition == "exhausted":
        assert isinstance(signal, GovernanceExhausted)
        assert runner.repository.exhausted == [(signal.code, False)]
    elif expected_transition == "integrity":
        assert isinstance(signal, GovernanceIntegrityError)
        assert runner.repository.exhausted == [(signal.code, True)]
    elif expected_transition == "yielded":
        assert runner.repository.yielded == [0]
    else:
        assert runner.repository.finish_cancelled_calls == 1


@pytest.mark.asyncio
async def test_question_context_database_error_has_no_provider_or_response_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _ResultAdapter(result=ModelGenerationResult(text="must not be used"))
    monkeypatch.setattr(
        "app.runners.evaluation_runner.build_adapter",
        lambda *args, **kwargs: adapter,
    )
    runner = _ManagedQuestionRunner(
        concurrency=1,
        question_count=1,
        governance=_managed_governance(),
    )
    runner._governance_repository = _FailingGovernanceRepository()
    runner._mock_generation_delay_seconds = 0

    assert await runner.execute("run-controlled") is True

    assert adapter.calls == []
    assert runner.repository.responses == []
    assert runner.repository.failed == []
    assert runner.repository.yielded == [0]
    assert runner.finished == []


@pytest.mark.parametrize(
    "signal",
    [
        GovernanceDeferred(
            "governance_global_concurrency",
            not_before=datetime(2026, 8, 27, 16, 0, tzinfo=UTC),
        ),
        GovernanceExhausted("governance_pricing_unknown"),
        GovernanceSettlementUnknown(),
        GovernanceFenceLost("governance_lease_fence_lost"),
    ],
)
@pytest.mark.asyncio
async def test_question_governance_signal_never_persists_zero_response(
    signal: BaseException,
) -> None:
    response_repository = _RecordingResponseRepository()
    runner = object.__new__(EvaluationRunner)
    runner._lease_repository = response_repository
    runner._governance_repository = object()
    runner._mock_generation_delay_seconds = 0
    model = _ModelSnapshot(
        provider_type="openai_compatible",
        base_url="https://provider.example/v1",
        remote_model_name="model",
        api_key_env="PROVIDER_KEY",
        input_price=None,
        output_price=None,
    )
    adapter = _ResultAdapter(error=signal)
    lease = RunLease(
        run_id="run-controlled",
        owner="worker-controlled",
        token=1,
        attempt=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )

    with pytest.raises(type(signal)) as caught:
        await runner._evaluate_question(lease, model, _question(), {}, {}, adapter)

    assert caught.value is signal
    assert response_repository.responses == []


@pytest.mark.asyncio
async def test_managed_question_uses_frozen_context_and_persists_safe_provider_evidence() -> None:
    context = ProviderAttemptContext(
        run_id="run-controlled",
        question_id="question-managed",
        model_id="model-managed",
        provider_scope="a" * 64,
        lease_token=1,
        execution_generation=3,
        next_provider_attempt=7,
        reserved_input_tokens=100,
        reserved_output_tokens=25,
        reserved_cost_usd=Decimal("0.00030000"),
    )
    governance_repository = _RecordingGovernanceRepository(context)
    response_repository = _RecordingResponseRepository()
    runner = object.__new__(EvaluationRunner)
    runner._lease_repository = response_repository
    runner._governance_repository = governance_repository
    runner._mock_generation_delay_seconds = 0
    model = _ModelSnapshot(
        provider_type="openai_compatible",
        base_url="https://provider.example/v1",
        remote_model_name="model",
        api_key_env="PROVIDER_KEY",
        input_price=Decimal("2"),
        output_price=Decimal("4"),
        governance=_managed_governance(),
    )
    generated = ModelGenerationResult(
        text="one",
        input_tokens=3,
        output_tokens=2,
        latency_ms=4.5,
        provider_request_id="request-123",
        metadata={
            "returned_model": "provider/model-1",
            "system_fingerprint": "fp_123",
            "finish_reason": "stop",
            "attempts": 3,
        },
    )
    adapter = _ResultAdapter(result=generated)
    lease = RunLease(
        run_id="run-controlled",
        owner="worker-controlled",
        token=1,
        attempt=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )

    disposition = await runner._evaluate_question(
        lease,
        model,
        _question(),
        {"max_tokens": 25},
        {},
        adapter,
    )

    assert disposition == ResponseDisposition.INSERTED
    context_call = governance_repository.question_context_calls[0]
    assert context_call["provider_scope"] == "a" * 64
    assert context_call["estimated_input_tokens"] > 0
    assert context_call["reserved_output_tokens"] == 25
    assert context_call["reserved_cost_usd"] == Decimal("0.0003")
    assert adapter.calls[0][2] == {"attempt_context": context}
    response = response_repository.responses[0]
    assert response.provider_request_id == "request-123"
    assert response.returned_model == "provider/model-1"
    assert response.system_fingerprint == "fp_123"
    assert response.finish_reason == "stop"
    assert response.http_attempt_count == 3


@pytest.mark.asyncio
async def test_legacy_question_keeps_old_call_shape_and_drops_unsafe_provider_evidence() -> None:
    response_repository = _RecordingResponseRepository()
    runner = object.__new__(EvaluationRunner)
    runner._lease_repository = response_repository
    runner._mock_generation_delay_seconds = 0
    model = _ModelSnapshot(
        provider_type="openai_compatible",
        base_url="https://provider.example/v1",
        remote_model_name="model",
        api_key_env="PROVIDER_KEY",
        input_price=None,
        output_price=None,
    )
    adapter = _ResultAdapter(
        result=ModelGenerationResult(
            text="one",
            provider_request_id="r" * 257,
            metadata={
                "returned_model": "model with space",
                "system_fingerprint": "fp\nbad",
                "finish_reason": 123,
                "attempts": True,
            },
        )
    )
    lease = RunLease(
        run_id="run-controlled",
        owner="worker-controlled",
        token=1,
        attempt=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )

    await runner._evaluate_question(lease, model, _question(), {}, {}, adapter)

    assert adapter.calls[0][2] == {}
    response = response_repository.responses[0]
    assert response.provider_request_id is None
    assert response.returned_model is None
    assert response.system_fingerprint is None
    assert response.finish_reason is None
    assert response.http_attempt_count is None
