"""Deterministic lease, fencing, retry, cancellation, and idempotency tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.session import create_database_engine
from app.models import (
    Benchmark,
    EvaluationResponse,
    EvaluationRun,
    Model,
    Question,
    RunStatus,
)
from app.runners.evaluation_runner import EvaluationRunner
from app.runners.run_leases import (
    AttemptDisposition,
    CancelDisposition,
    ResponseDisposition,
    RunLeaseRepository,
)


@dataclass
class FixedDatabaseClock:
    current: datetime

    def __call__(self, _session: Session) -> datetime:
        return self.current

    def advance(self, **changes: float) -> None:
        self.current += timedelta(**changes)


@pytest.fixture
def lease_store(tmp_path: Path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'leases.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )
    with factory() as session, session.begin():
        model = Model(id="model-1", name="Lease Mock", provider_type="mock")
        benchmark = Benchmark(
            id="benchmark-1",
            slug="lease-fixture",
            name="Lease fixture",
            version="1.0.0",
            description="fixture",
            dimension="general",
            language="en",
            license="MIT",
            source="local",
            evaluator_type="exact_match",
            evaluator_config={},
            prompt_template={},
            dataset_hash="lease-hash",
            question_count=1,
        )
        question = Question(
            id="question-1",
            benchmark_id=benchmark.id,
            external_id="q1",
            position=0,
            question_type="exact_match",
            prompt="One?",
            reference_answer="one",
        )
        run = EvaluationRun(
            id="run-1",
            model_id=model.id,
            benchmark_id=benchmark.id,
            status=RunStatus.PENDING,
            model_parameters_snapshot={},
            benchmark_hash_snapshot=benchmark.dataset_hash,
            prompt_template_snapshot={},
            total_questions=1,
        )
        session.add_all([model, benchmark, question, run])
    try:
        yield factory
    finally:
        engine.dispose()


def _repository(
    factory: sessionmaker[Session],
    clock: FixedDatabaseClock,
) -> RunLeaseRepository:
    return RunLeaseRepository(
        factory,
        lease_for=timedelta(seconds=30),
        retry_backoff_base=timedelta(seconds=2),
        retry_backoff_cap=timedelta(seconds=8),
        clock=clock,
    )


def _response() -> EvaluationResponse:
    return EvaluationResponse(
        run_id="run-1",
        question_id="question-1",
        raw_response="one",
        parsed_answer="one",
        reference_answer_snapshot="one",
        score=1,
        evaluator_name="exact_match_v1",
    )


def test_only_one_concurrent_claim_succeeds(lease_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 25, 4, 0, tzinfo=UTC))
    repository = _repository(lease_store, clock)
    barrier = Barrier(2)

    def compete(owner: str):
        barrier.wait(timeout=5)
        return repository.claim("run-1", owner=owner)

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(compete, ("worker-a", "worker-b")))

    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 1
    assert claimed[0].token == 1
    assert claimed[0].attempt == 1
    with lease_store() as session:
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        assert run.status == RunStatus.RUNNING
        assert run.attempt_count == 1
        assert run.lease_owner == claimed[0].owner


def test_sqlite_claim_and_cancel_race_preserves_state_constraints(lease_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 25, 4, 0, tzinfo=UTC))
    repository = _repository(lease_store, clock)
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return repository.claim("run-1", owner="worker-a")

    def cancel():
        barrier.wait(timeout=5)
        return repository.request_cancel("run-1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(claim)
        cancel_future = executor.submit(cancel)
        lease = claim_future.result(timeout=10)
        disposition = cancel_future.result(timeout=10)

    with lease_store() as session:
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        assert run.cancellation_requested is True
        if lease is None:
            assert disposition == CancelDisposition.CANCELLED
            assert run.status == RunStatus.CANCELLED
            assert run.lease_owner is None
            assert run.lease_expires_at is None
            assert run.heartbeat_at is None
        else:
            assert disposition == CancelDisposition.REQUESTED
            assert run.status == RunStatus.RUNNING
            assert run.lease_owner == lease.owner
            assert run.lease_expires_at is not None
            assert run.heartbeat_at is not None


def test_sqlite_retry_and_cancel_race_cannot_lose_cancellation(lease_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 25, 4, 0, tzinfo=UTC))
    repository = _repository(lease_store, clock)
    with lease_store() as session, session.begin():
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        run.max_attempts = 2
    lease = repository.claim("run-1", owner="worker-a")
    assert lease is not None
    barrier = Barrier(2)

    def fail():
        barrier.wait(timeout=5)
        return repository.fail_attempt(lease, error_code="worker_interrupted")

    def cancel():
        barrier.wait(timeout=5)
        return repository.request_cancel("run-1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        fail_future = executor.submit(fail)
        cancel_future = executor.submit(cancel)
        attempt_disposition = fail_future.result(timeout=10)
        cancel_disposition = cancel_future.result(timeout=10)

    assert attempt_disposition in {
        AttemptDisposition.CANCELLED,
        AttemptDisposition.RETRY_SCHEDULED,
    }
    assert cancel_disposition in {
        CancelDisposition.CANCELLED,
        CancelDisposition.REQUESTED,
    }
    with lease_store() as session:
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        assert run.status == RunStatus.CANCELLED
        assert run.cancellation_requested is True
        assert run.next_attempt_at is None
        assert run.lease_owner is None


def test_sqlite_finish_and_cancel_race_resolves_to_one_terminal_state(lease_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 25, 4, 0, tzinfo=UTC))
    repository = _repository(lease_store, clock)
    lease = repository.claim("run-1", owner="worker-a")
    assert lease is not None
    assert repository.persist_response(lease, _response()) == ResponseDisposition.INSERTED
    runner = EvaluationRunner(
        lease_store,
        worker_id=lease.owner,
        lease_repository=repository,
    )
    barrier = Barrier(2)

    def finish():
        barrier.wait(timeout=5)
        return runner._finish(lease, RunStatus.COMPLETED)

    def cancel():
        barrier.wait(timeout=5)
        return repository.request_cancel("run-1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        finish_future = executor.submit(finish)
        cancel_future = executor.submit(cancel)
        assert finish_future.result(timeout=10) in {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
        }
        cancel_disposition = cancel_future.result(timeout=10)

    assert cancel_disposition in {
        CancelDisposition.REQUESTED,
        CancelDisposition.TERMINAL,
    }
    with lease_store() as session:
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        assert run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}
        assert run.lease_owner is None
        assert run.lease_expires_at is None
        assert run.heartbeat_at is None
        if run.cancellation_requested:
            assert run.status == RunStatus.CANCELLED


def test_heartbeat_takeover_and_response_writes_are_fenced(lease_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 25, 4, 0, tzinfo=UTC))
    repository = _repository(lease_store, clock)
    first = repository.claim("run-1", owner="worker-a")
    assert first is not None
    assert first.expires_at == clock.current + timedelta(seconds=30)
    assert repository.claim("run-1", owner="worker-b") is None

    clock.advance(seconds=10)
    renewed = repository.heartbeat(first)
    assert renewed is not None
    assert renewed.expires_at == clock.current + timedelta(seconds=30)

    clock.advance(seconds=31)
    second = repository.claim("run-1", owner="worker-b")
    assert second is not None
    assert second.token == 2
    assert second.attempt == 2
    assert repository.heartbeat(first) is None
    assert repository.persist_response(first, _response()) == ResponseDisposition.FENCE_LOST

    assert repository.persist_response(second, _response()) == ResponseDisposition.INSERTED
    assert repository.persist_response(second, _response()) == ResponseDisposition.ALREADY_PRESENT
    with lease_store() as session:
        response_count = session.scalar(
            select(func.count(EvaluationResponse.id)).where(EvaluationResponse.run_id == "run-1")
        )
        run = session.get(EvaluationRun, "run-1")
        assert response_count == 1
        assert run is not None and run.completed_questions == 1


def test_retry_backoff_and_dead_letter_are_finite(lease_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 25, 4, 0, tzinfo=UTC))
    repository = _repository(lease_store, clock)
    with lease_store() as session, session.begin():
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        run.max_attempts = 2

    first = repository.claim("run-1", owner="worker-a")
    assert first is not None
    assert (
        repository.fail_attempt(first, error_code="database_temporarily_unavailable")
        == AttemptDisposition.RETRY_SCHEDULED
    )
    assert repository.claim("run-1", owner="worker-b") is None

    clock.advance(seconds=2)
    second = repository.claim("run-1", owner="worker-b")
    assert second is not None and second.attempt == 2
    assert (
        repository.fail_attempt(second, error_code="database_temporarily_unavailable")
        == AttemptDisposition.DEAD_LETTERED
    )
    assert repository.claim("run-1", owner="worker-c") is None
    with lease_store() as session:
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        assert run.status == RunStatus.FAILED
        assert run.attempt_count == run.max_attempts == 2
        assert run.dead_lettered_at == clock.current
        assert run.last_error == "database_temporarily_unavailable"
        assert run.lease_owner is None
        assert run.lease_expires_at is None
        assert run.heartbeat_at is None


def test_pending_and_running_cancellation_converge_deterministically(lease_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 25, 4, 0, tzinfo=UTC))
    repository = _repository(lease_store, clock)

    assert repository.request_cancel("run-1") == CancelDisposition.CANCELLED
    assert repository.claim("run-1", owner="worker-a") is None
    assert repository.request_cancel("run-1") == CancelDisposition.TERMINAL

    with lease_store() as session, session.begin():
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        run.status = RunStatus.PENDING
        run.cancellation_requested = False
        run.finished_at = None

    lease = repository.claim("run-1", owner="worker-a")
    assert lease is not None
    assert repository.request_cancel("run-1") == CancelDisposition.REQUESTED
    assert repository.persist_response(lease, _response()) == ResponseDisposition.CANCEL_REQUESTED
    assert repository.finish_cancelled(lease) is True
    with lease_store() as session:
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        assert run.status == RunStatus.CANCELLED
        assert run.completed_questions == 0
        assert run.lease_owner is None


def test_pending_retry_cancellation_clears_backoff_and_aggregates_evidence(lease_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 25, 4, 0, tzinfo=UTC))
    repository = _repository(lease_store, clock)
    with lease_store() as session, session.begin():
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        run.max_attempts = 2
        run.total_questions = 2

    lease = repository.claim("run-1", owner="worker-a")
    assert lease is not None
    assert repository.persist_response(lease, _response()) == ResponseDisposition.INSERTED
    assert (
        repository.fail_attempt(lease, error_code="worker_interrupted")
        == AttemptDisposition.RETRY_SCHEDULED
    )

    assert repository.request_cancel("run-1") == CancelDisposition.CANCELLED
    with lease_store() as session:
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        assert run.status == RunStatus.CANCELLED
        assert run.next_attempt_at is None
        assert run.completed_questions == run.correct_questions == 1
        assert run.score == run.completion_rate == 50.0
        assert run.answered_accuracy == 100.0


@pytest.mark.parametrize("max_attempts", [1, 3])
def test_attempt_failure_with_complete_evidence_recovers_completed(
    lease_store,
    max_attempts: int,
) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 25, 4, 0, tzinfo=UTC))
    repository = _repository(lease_store, clock)
    with lease_store() as session, session.begin():
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        run.max_attempts = max_attempts

    lease = repository.claim("run-1", owner="worker-a")
    assert lease is not None
    assert repository.persist_response(lease, _response()) == ResponseDisposition.INSERTED
    assert (
        repository.fail_attempt(lease, error_code="crashed_before_finish")
        == AttemptDisposition.RECOVERED_COMPLETED
    )
    with lease_store() as session:
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        assert run.dead_lettered_at is None
        assert run.completed_questions == run.total_questions == 1
        assert run.score == 100.0


@pytest.mark.parametrize("max_attempts", [1, 3])
def test_reaper_completes_expired_attempt_from_full_evidence(
    lease_store,
    max_attempts: int,
) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 25, 4, 0, tzinfo=UTC))
    repository = _repository(lease_store, clock)
    with lease_store() as session, session.begin():
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        run.max_attempts = max_attempts

    lease = repository.claim("run-1", owner="worker-a")
    assert lease is not None
    assert repository.persist_response(lease, _response()) == ResponseDisposition.INSERTED
    clock.advance(seconds=31)
    assert repository.claim("run-1", owner="worker-b") is None
    with lease_store() as session:
        before_reap = session.get(EvaluationRun, "run-1")
        assert before_reap is not None and before_reap.attempt_count == 1

    report = repository.reap_expired()

    assert report.completed == 1
    assert report.cancelled == report.dead_lettered == 0
    with lease_store() as session:
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        assert run.dead_lettered_at is None
        assert run.completed_questions == run.total_questions == 1
        assert run.lease_owner is None
    assert repository.persist_response(lease, _response()) == ResponseDisposition.FENCE_LOST


@pytest.mark.parametrize("cancel_requested", [False, True])
def test_reaper_settles_exhausted_or_cancelled_expired_lease(
    lease_store,
    cancel_requested: bool,
) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 25, 4, 0, tzinfo=UTC))
    repository = _repository(lease_store, clock)
    with lease_store() as session, session.begin():
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        run.max_attempts = 1

    lease = repository.claim("run-1", owner="worker-a")
    assert lease is not None
    if cancel_requested:
        assert repository.request_cancel("run-1") == CancelDisposition.REQUESTED
    clock.advance(seconds=31)

    report = repository.reap_expired()

    with lease_store() as session:
        run = session.get(EvaluationRun, "run-1")
        assert run is not None
        if cancel_requested:
            assert report.cancelled == 1
            assert report.dead_lettered == 0
            assert report.completed == 0
            assert run.status == RunStatus.CANCELLED
        else:
            assert report.cancelled == 0
            assert report.dead_lettered == 1
            assert report.completed == 0
            assert run.status == RunStatus.FAILED
            assert run.dead_lettered_at == clock.current
        assert run.lease_owner is None
        assert run.lease_expires_at is None
        assert run.heartbeat_at is None
    assert repository.persist_response(lease, _response()) == ResponseDisposition.FENCE_LOST
