"""Real PostgreSQL lease and fencing evidence; skipped without an explicit test DSN."""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import ProviderAttemptDisposition, ProviderAttemptOutcome
from app.api.v1.models import _active_run_exists
from app.db.clock import database_utc_now
from app.db.session import create_database_engine
from app.governance import (
    AuditIntegrityError,
    GovernanceBacklogFull,
    GovernanceDeferred,
    GovernanceExhausted,
    GovernanceRepository,
    append_audit_event,
    provider_scope_key,
)
from app.models import (
    AuditEvent,
    Benchmark,
    CredentialSource,
    EvaluationResponse,
    EvaluationRun,
    GovernanceMinuteBucket,
    GovernancePolicy,
    GovernanceRunStatus,
    GovernanceScope,
    GovernanceScopeType,
    Model,
    ProviderCallReservation,
    ProviderCallReservationState,
    ProviderType,
    Question,
    RunStatus,
    WorkerProcess,
)
from app.runners.run_leases import CancelDisposition, ResponseDisposition, RunLeaseRepository
from app.worker_progress import (
    WorkerProgressEvent,
    WorkerProgressRecorder,
    collect_worker_progress,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def postgres_store():
    database_url = os.environ.get("LLMBENCHLAB_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("LLMBENCHLAB_TEST_POSTGRES_URL is required")
    if make_url(database_url).get_backend_name() != "postgresql":
        pytest.fail("LLMBENCHLAB_TEST_POSTGRES_URL must use PostgreSQL")
    database_name = make_url(database_url).database or ""
    if (
        "test" not in database_name.lower()
        and os.environ.get("LLMBENCHLAB_TEST_ALLOW_TRUNCATE") != "1"
    ):
        pytest.fail(
            "PostgreSQL integration tests require a database name containing 'test' or "
            "LLMBENCHLAB_TEST_ALLOW_TRUNCATE=1 because the fixture truncates its tables"
        )
    engine = create_database_engine(database_url)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE worker_processes, audit_events, provider_call_reservations, "
                "question_executions, "
                "governance_minute_buckets, governance_scopes, evaluation_responses, "
                "evaluation_runs, questions, benchmarks, models, governance_policies "
                "RESTART IDENTITY CASCADE"
            )
        )
    with factory() as session, session.begin():
        model = Model(id="model-pg", name="Postgres Lease Mock", provider_type="mock")
        benchmark = Benchmark(
            id="benchmark-pg",
            slug="postgres-lease-fixture",
            name="PostgreSQL lease fixture",
            version="1.0.0",
            description="fixture",
            dimension="general",
            language="en",
            license="MIT",
            source="local",
            evaluator_type="exact_match",
            evaluator_config={},
            prompt_template={},
            dataset_hash="postgres-lease-hash",
            question_count=1,
        )
        session.add_all(
            [
                model,
                benchmark,
                Question(
                    id="question-pg",
                    benchmark_id=benchmark.id,
                    external_id="q1",
                    position=0,
                    question_type="exact_match",
                    prompt="One?",
                    reference_answer="one",
                ),
                EvaluationRun(
                    id="run-pg",
                    model_id=model.id,
                    benchmark_id=benchmark.id,
                    status=RunStatus.PENDING,
                    model_parameters_snapshot={},
                    benchmark_hash_snapshot=benchmark.dataset_hash,
                    prompt_template_snapshot={},
                    total_questions=1,
                ),
            ]
        )
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_postgres_worker_generations_flush_and_stop_without_cross_talk(
    postgres_store,
) -> None:
    recorders = [
        WorkerProgressRecorder(
            postgres_store,
            worker_id=f"worker-progress-pg-{index}",
            flush_seconds=60,
        )
        for index in range(2)
    ]
    await asyncio.gather(*(recorder.start() for recorder in recorders))
    recorders[0].note(WorkerProgressEvent.SCAN)
    recorders[1].note(WorkerProgressEvent.PROGRESS)
    assert await asyncio.gather(*(recorder.flush_now() for recorder in recorders)) == [True, True]

    with postgres_store() as session:
        now = database_utc_now(session)
        snapshot = collect_worker_progress(
            session,
            now=now,
            stale_seconds=60,
            expected=2,
        )
        assert snapshot.registered == 2
        assert snapshot.live == 2
        assert snapshot.stalled == 0
        assert snapshot.shortfall == 0

    assert await asyncio.gather(*(recorder.stop() for recorder in recorders)) == [True, True]
    with postgres_store() as session:
        assert (
            session.scalar(
                select(func.count(WorkerProcess.generation_id)).where(
                    WorkerProcess.stopped_at.is_not(None)
                )
            )
            == 2
        )


def _response() -> EvaluationResponse:
    return EvaluationResponse(
        run_id="run-pg",
        question_id="question-pg",
        raw_response="one",
        parsed_answer="one",
        reference_answer_snapshot="one",
        score=1,
        evaluator_name="exact_match_v1",
    )


def _full_policy(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "global_concurrency_limit": None,
        "provider_concurrency_limit": None,
        "model_concurrency_limit": None,
        "run_concurrency_limit": None,
        "global_requests_per_minute": None,
        "provider_requests_per_minute": None,
        "model_requests_per_minute": None,
        "run_requests_per_minute": None,
        "global_tokens_per_minute": None,
        "provider_tokens_per_minute": None,
        "model_tokens_per_minute": None,
        "run_tokens_per_minute": None,
        "global_lifetime_request_budget": None,
        "global_lifetime_token_budget": None,
        "global_lifetime_cost_budget_usd": None,
        "run_lifetime_request_budget": None,
        "run_lifetime_token_budget": None,
        "run_lifetime_cost_budget_usd": None,
        "backlog_limit": 1000,
        "question_quantum": 25,
    }
    values.update(overrides)
    return values


def _prepare_atomic_governance_attempts(
    postgres_store,
    *,
    policy_overrides: dict[str, object],
    same_run: bool,
):
    """Create two independently reservable attempts sharing the requested scope."""

    governance = GovernanceRepository(postgres_store)
    lease_repository = RunLeaseRepository(postgres_store, lease_for=timedelta(seconds=30))
    run_ids = (
        ("run-atomic-pg-a",)
        if same_run
        else (
            "run-atomic-pg-a",
            "run-atomic-pg-b",
        )
    )
    with postgres_store() as session, session.begin():
        governance.apply_policy(session, _full_policy(**policy_overrides))
        if same_run:
            session.add(
                Question(
                    id="question-pg-atomic-2",
                    benchmark_id="benchmark-pg",
                    external_id="q-atomic-2",
                    position=1,
                    question_type="exact_match",
                    prompt="Two?",
                    reference_answer="two",
                )
            )
        for run_id in run_ids:
            governance.admit_run(
                session,
                EvaluationRun(
                    id=run_id,
                    model_id="model-pg",
                    benchmark_id="benchmark-pg",
                    status=RunStatus.PENDING,
                    model_parameters_snapshot={"execution": {"retry_policy": {"max_attempts": 1}}},
                    benchmark_hash_snapshot="postgres-lease-hash",
                    prompt_template_snapshot={},
                    total_questions=2 if same_run else 1,
                    input_token_reservation=4,
                ),
                provider_type="mock",
                base_url=None,
            )

    claimed = [
        lease_repository.claim(run_id, owner=f"worker-atomic-pg-{index}")
        for index, run_id in enumerate(run_ids)
    ]
    assert all(lease is not None for lease in claimed)
    leases = [lease for lease in claimed if lease is not None]
    if same_run:
        attempt_leases = (leases[0], leases[0])
        question_ids = ("question-pg", "question-pg-atomic-2")
    else:
        attempt_leases = (leases[0], leases[1])
        question_ids = ("question-pg", "question-pg")
    contexts = tuple(
        governance.question_context(
            run_id=lease.run_id,
            question_id=question_id,
            model_id="model-pg",
            provider_scope=provider_scope_key("mock", None),
            lease_owner=lease.owner,
            lease_token=lease.token,
            estimated_input_tokens=4,
            reserved_output_tokens=2,
            reserved_cost_usd=Decimal("0.10000000"),
        )
        for lease, question_id in zip(attempt_leases, question_ids, strict=True)
    )
    return governance, attempt_leases, contexts


def test_postgres_concurrent_policy_apply_serializes_versions(postgres_store) -> None:
    repository = GovernanceRepository(postgres_store)
    barrier = Barrier(2)
    policies = (
        _full_policy(backlog_limit=111, question_quantum=7),
        _full_policy(backlog_limit=222, question_quantum=9),
    )

    def apply(values: dict[str, object]) -> tuple[str, int]:
        with postgres_store() as session, session.begin():
            barrier.wait(timeout=5)
            policy = repository.apply_policy(session, values)
            return policy.id, policy.version

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply, values) for values in policies]
        applied = [future.result(timeout=10) for future in futures]

    assert {version for _policy_id, version in applied} == {2, 3}
    with postgres_store() as session:
        assert session.scalar(select(func.count(GovernancePolicy.id))) == 3
        assert (
            session.scalar(
                select(func.count(GovernancePolicy.id)).where(GovernancePolicy.is_active.is_(True))
            )
            == 1
        )


def test_postgres_partial_unique_index_rejects_concurrent_active_rows(postgres_store) -> None:
    now = datetime.now(UTC)
    barrier = Barrier(2)

    def insert_active(index: int) -> str:
        try:
            with postgres_store() as session, session.begin():
                barrier.wait(timeout=5)
                session.add(
                    GovernancePolicy(
                        id=f"policy-pg-direct-{index}",
                        version=index,
                        policy_hash=str(index) * 64,
                        is_active=True,
                        activated_at=now,
                        created_at=now,
                        **_full_policy(backlog_limit=100 + index),
                    )
                )
                session.flush()
            return "inserted"
        except IntegrityError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(insert_active, (1, 2)))

    assert sorted(results) == ["inserted", "rejected"]
    with postgres_store() as session:
        assert (
            session.scalar(
                select(func.count(GovernancePolicy.id)).where(GovernancePolicy.is_active.is_(True))
            )
            == 1
        )


def test_postgres_concurrent_claim_takeover_and_fencing(postgres_store) -> None:
    repository = RunLeaseRepository(postgres_store, lease_for=timedelta(seconds=30))
    barrier = Barrier(2)

    def compete(owner: str):
        barrier.wait(timeout=5)
        return repository.claim("run-pg", owner=owner)

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(compete, ("worker-pg-a", "worker-pg-b")))

    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 1
    first = claimed[0]
    assert first.attempt == first.token == 1

    with postgres_store() as session, session.begin():
        session.execute(
            text(
                "UPDATE evaluation_runs SET lease_expires_at = "
                "CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE id = 'run-pg'"
            )
        )
    second_owner = "worker-pg-b" if first.owner == "worker-pg-a" else "worker-pg-a"
    second = repository.claim("run-pg", owner=second_owner)
    assert second is not None
    assert second.attempt == second.token == 2

    assert repository.heartbeat(first) is None
    assert repository.persist_response(first, _response()) == ResponseDisposition.FENCE_LOST
    assert repository.persist_response(second, _response()) == ResponseDisposition.INSERTED
    assert repository.persist_response(second, _response()) == ResponseDisposition.ALREADY_PRESENT

    with postgres_store() as session:
        run = session.get(EvaluationRun, "run-pg")
        response_count = session.scalar(
            select(func.count(EvaluationResponse.id)).where(EvaluationResponse.run_id == "run-pg")
        )
        assert run is not None
        assert run.lease_owner == second.owner
        assert run.lease_token == 2
        assert run.attempt_count == 2
        assert run.completed_questions == response_count == 1


def test_postgres_claim_and_cancel_race_preserves_state_constraints(postgres_store) -> None:
    repository = RunLeaseRepository(postgres_store, lease_for=timedelta(seconds=30))
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return repository.claim("run-pg", owner="worker-pg")

    def cancel():
        barrier.wait(timeout=5)
        return repository.request_cancel("run-pg")

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(claim)
        cancel_future = executor.submit(cancel)
        lease = claim_future.result(timeout=10)
        disposition = cancel_future.result(timeout=10)

    with postgres_store() as session:
        run = session.get(EvaluationRun, "run-pg")
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


@pytest.mark.parametrize(
    ("scope_type", "limit_field", "same_run"),
    [
        (GovernanceScopeType.GLOBAL, "global_concurrency_limit", False),
        (GovernanceScopeType.PROVIDER, "provider_concurrency_limit", False),
        (GovernanceScopeType.MODEL, "model_concurrency_limit", False),
        (GovernanceScopeType.RUN, "run_concurrency_limit", True),
    ],
)
def test_postgres_governance_concurrency_limits_are_atomic_across_connections(
    postgres_store,
    scope_type: GovernanceScopeType,
    limit_field: str,
    same_run: bool,
) -> None:
    governance = GovernanceRepository(postgres_store)
    lease_repository = RunLeaseRepository(postgres_store, lease_for=timedelta(seconds=30))
    run_ids = (
        ("run-governance-a",)
        if same_run
        else (
            "run-governance-a",
            "run-governance-b",
        )
    )
    with postgres_store() as session, session.begin():
        governance.apply_policy(
            session,
            _full_policy(backlog_limit=100, question_quantum=2, **{limit_field: 1}),
        )
        if same_run:
            session.add(
                Question(
                    id="question-pg-2",
                    benchmark_id="benchmark-pg",
                    external_id="q2",
                    position=1,
                    question_type="exact_match",
                    prompt="Two?",
                    reference_answer="two",
                )
            )
        for run_id in run_ids:
            governance.admit_run(
                session,
                EvaluationRun(
                    id=run_id,
                    model_id="model-pg",
                    benchmark_id="benchmark-pg",
                    status=RunStatus.PENDING,
                    model_parameters_snapshot={"execution": {"retry_policy": {"max_attempts": 1}}},
                    benchmark_hash_snapshot="postgres-lease-hash",
                    prompt_template_snapshot={},
                    total_questions=2 if same_run else 1,
                ),
                provider_type="mock",
                base_url=None,
            )

    leases = [
        lease_repository.claim(run_id, owner=f"worker-{index}")
        for index, run_id in enumerate(run_ids)
    ]
    assert all(lease is not None for lease in leases)
    if same_run:
        leases.append(leases[0])
        question_ids = ("question-pg", "question-pg-2")
    else:
        question_ids = ("question-pg", "question-pg")
    contexts = [
        governance.question_context(
            run_id=lease.run_id,
            question_id=question_id,
            model_id="model-pg",
            provider_scope=provider_scope_key("mock", None),
            lease_owner=lease.owner,
            lease_token=lease.token,
            estimated_input_tokens=4,
            reserved_output_tokens=2,
            reserved_cost_usd=None,
        )
        for lease, question_id in zip(leases, question_ids, strict=True)
        if lease is not None
    ]
    barrier = Barrier(2)

    def reserve(index: int):
        barrier.wait(timeout=5)
        try:
            return governance.reserve(
                contexts[index],
                provider_attempt=1,
                lease_owner=leases[index].owner,
            )
        except GovernanceDeferred as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, (0, 1)))

    winner = next(
        index for index, result in enumerate(results) if not isinstance(result, Exception)
    )
    loser = 1 - winner
    assert isinstance(results[loser], GovernanceDeferred)
    assert results[loser].code == f"governance_{scope_type.value}_concurrency"
    with postgres_store() as session:
        scope = session.scalar(
            select(GovernanceScope).where(GovernanceScope.scope_type == scope_type)
        )
        assert scope is not None and scope.active_reservations == 1

    permit = results[winner]
    governance.mark_send_started(permit, lease_owner=leases[winner].owner)
    governance.finish(
        permit,
        disposition=ProviderAttemptDisposition.SETTLED_ACTUAL,
        outcome=ProviderAttemptOutcome.SUCCEEDED,
        input_tokens=4,
        output_tokens=2,
        actual_cost_usd=None,
    )
    retry_permit = governance.reserve(
        contexts[loser],
        provider_attempt=1,
        lease_owner=leases[loser].owner,
    )
    governance.finish(
        retry_permit,
        disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
        outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )


@pytest.mark.parametrize(
    ("limit_field", "limit_value", "same_run", "signal_type", "expected_code"),
    [
        pytest.param(
            "global_requests_per_minute",
            1,
            False,
            GovernanceDeferred,
            "governance_global_rpm",
            id="global-rpm",
        ),
        pytest.param(
            "provider_requests_per_minute",
            1,
            False,
            GovernanceDeferred,
            "governance_provider_rpm",
            id="provider-rpm",
        ),
        pytest.param(
            "model_requests_per_minute",
            1,
            False,
            GovernanceDeferred,
            "governance_model_rpm",
            id="model-rpm",
        ),
        pytest.param(
            "run_requests_per_minute",
            1,
            True,
            GovernanceDeferred,
            "governance_run_rpm",
            id="run-rpm",
        ),
        pytest.param(
            "global_tokens_per_minute",
            6,
            False,
            GovernanceDeferred,
            "governance_global_tpm",
            id="global-tpm",
        ),
        pytest.param(
            "provider_tokens_per_minute",
            6,
            False,
            GovernanceDeferred,
            "governance_provider_tpm",
            id="provider-tpm",
        ),
        pytest.param(
            "model_tokens_per_minute",
            6,
            False,
            GovernanceDeferred,
            "governance_model_tpm",
            id="model-tpm",
        ),
        pytest.param(
            "run_tokens_per_minute",
            6,
            True,
            GovernanceDeferred,
            "governance_run_tpm",
            id="run-tpm",
        ),
        pytest.param(
            "global_lifetime_request_budget",
            1,
            False,
            GovernanceExhausted,
            "governance_global_request_budget_exhausted",
            id="global-lifetime-requests",
        ),
        pytest.param(
            "run_lifetime_request_budget",
            1,
            True,
            GovernanceExhausted,
            "governance_run_request_budget_exhausted",
            id="run-lifetime-requests",
        ),
        pytest.param(
            "global_lifetime_token_budget",
            6,
            False,
            GovernanceExhausted,
            "governance_global_token_budget_exhausted",
            id="global-lifetime-tokens",
        ),
        pytest.param(
            "run_lifetime_token_budget",
            6,
            True,
            GovernanceExhausted,
            "governance_run_token_budget_exhausted",
            id="run-lifetime-tokens",
        ),
        pytest.param(
            "global_lifetime_cost_budget_usd",
            Decimal("0.10000000"),
            False,
            GovernanceExhausted,
            "governance_global_cost_budget_exhausted",
            id="global-lifetime-cost",
        ),
        pytest.param(
            "run_lifetime_cost_budget_usd",
            Decimal("0.10000000"),
            True,
            GovernanceExhausted,
            "governance_run_cost_budget_exhausted",
            id="run-lifetime-cost",
        ),
    ],
)
def test_postgres_governance_rate_and_budget_limits_are_atomic_across_connections(
    postgres_store,
    limit_field: str,
    limit_value: object,
    same_run: bool,
    signal_type: type[GovernanceDeferred | GovernanceExhausted],
    expected_code: str,
) -> None:
    governance, leases, contexts = _prepare_atomic_governance_attempts(
        postgres_store,
        policy_overrides={limit_field: limit_value},
        same_run=same_run,
    )
    barrier = Barrier(2)

    def reserve(index: int):
        barrier.wait(timeout=5)
        try:
            return governance.reserve(
                contexts[index],
                provider_attempt=1,
                lease_owner=leases[index].owner,
            )
        except (GovernanceDeferred, GovernanceExhausted) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve, index) for index in (0, 1)]
        results = [future.result(timeout=10) for future in futures]

    winners = [index for index, result in enumerate(results) if not isinstance(result, Exception)]
    assert len(winners) == 1
    winner = winners[0]
    loser = 1 - winner
    assert isinstance(results[loser], signal_type)
    assert results[loser].code == expected_code

    winning_scope_identities = {
        (GovernanceScopeType.GLOBAL, "global"),
        (GovernanceScopeType.PROVIDER, provider_scope_key("mock", None)),
        (GovernanceScopeType.MODEL, "model-pg"),
        (GovernanceScopeType.RUN, contexts[winner].run_id),
    }
    with postgres_store() as session:
        reservations = list(session.scalars(select(ProviderCallReservation)))
        assert len(reservations) == 1
        assert reservations[0].id == results[winner].reservation_id
        assert reservations[0].state == ProviderCallReservationState.RESERVED

        scopes = list(session.scalars(select(GovernanceScope)))
        winning_scope_ids = {
            scope.id
            for scope in scopes
            if (scope.scope_type, scope.scope_key) in winning_scope_identities
        }
        assert len(winning_scope_ids) == 4
        for scope in scopes:
            expected = 1 if scope.id in winning_scope_ids else 0
            assert scope.active_reservations == expected
            assert scope.reserved_requests == expected
            assert scope.reserved_input_tokens == expected * 4
            assert scope.reserved_output_tokens == expected * 2
            assert scope.reserved_cost_usd == Decimal(expected) * Decimal("0.10000000")
            assert scope.consumed_requests == 0
            assert scope.consumed_input_tokens == 0
            assert scope.consumed_output_tokens == 0
            assert scope.consumed_cost_usd == 0

        buckets = list(session.scalars(select(GovernanceMinuteBucket)))
        assert len(buckets) == len(scopes)
        for bucket in buckets:
            expected = 1 if bucket.scope_id in winning_scope_ids else 0
            assert bucket.reserved_requests == expected
            assert bucket.reserved_input_tokens == expected * 4
            assert bucket.reserved_output_tokens == expected * 2
            assert bucket.consumed_requests == 0
            assert bucket.consumed_input_tokens == 0
            assert bucket.consumed_output_tokens == 0

    governance.finish(
        results[winner],
        disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
        outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )
    with postgres_store() as session:
        scopes = list(session.scalars(select(GovernanceScope)))
        assert all(scope.active_reservations == 0 for scope in scopes)
        assert all(scope.reserved_requests == 0 for scope in scopes)
        assert all(scope.reserved_input_tokens == 0 for scope in scopes)
        assert all(scope.reserved_output_tokens == 0 for scope in scopes)
        assert all(scope.reserved_cost_usd == 0 for scope in scopes)
        buckets = list(session.scalars(select(GovernanceMinuteBucket)))
        assert all(bucket.reserved_requests == 0 for bucket in buckets)
        assert all(bucket.reserved_input_tokens == 0 for bucket in buckets)
        assert all(bucket.reserved_output_tokens == 0 for bucket in buckets)


def test_postgres_concurrent_backlog_admission_enforces_exact_limit(postgres_store) -> None:
    governance = GovernanceRepository(postgres_store)
    backlog_limit = 3
    contender_count = 8
    with postgres_store() as session, session.begin():
        policy = governance.apply_policy(
            session,
            _full_policy(backlog_limit=backlog_limit),
        )
    assert policy.backlog_limit == backlog_limit
    barrier = Barrier(contender_count)

    def admit(index: int):
        run = EvaluationRun(
            id=f"run-backlog-pg-{index}",
            model_id="model-pg",
            benchmark_id="benchmark-pg",
            status=RunStatus.PENDING,
            model_parameters_snapshot={},
            benchmark_hash_snapshot="postgres-lease-hash",
            prompt_template_snapshot={},
            total_questions=1,
        )
        barrier.wait(timeout=10)
        try:
            with postgres_store() as session, session.begin():
                governance.admit_run(
                    session,
                    run,
                    provider_type="mock",
                    base_url=None,
                )
            return run.id
        except GovernanceBacklogFull as exc:
            return exc

    with ThreadPoolExecutor(max_workers=contender_count) as executor:
        futures = [executor.submit(admit, index) for index in range(contender_count)]
        results = [future.result(timeout=20) for future in futures]

    admitted = {result for result in results if isinstance(result, str)}
    rejected = [result for result in results if isinstance(result, GovernanceBacklogFull)]
    assert len(admitted) == backlog_limit
    assert len(rejected) == contender_count - backlog_limit
    assert all(error.code == "run_backlog_full" for error in rejected)
    assert all(error.limit == backlog_limit for error in rejected)

    with postgres_store() as session:
        persisted = set(
            session.scalars(
                select(EvaluationRun.id).where(EvaluationRun.id.like("run-backlog-pg-%"))
            )
        )
        assert persisted == admitted
        assert (
            session.scalar(
                select(func.count(EvaluationRun.id)).where(
                    EvaluationRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
                    EvaluationRun.governance_status == GovernanceRunStatus.MANAGED,
                )
            )
            == backlog_limit
        )
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.event_type == "run_admitted")
            )
            == backlog_limit
        )


def test_postgres_settlement_and_reconcile_race_has_one_idempotent_terminal_fact(
    postgres_store,
) -> None:
    governance, leases, contexts = _prepare_atomic_governance_attempts(
        postgres_store,
        policy_overrides={},
        same_run=True,
    )
    lease = leases[0]
    permit = governance.reserve(
        contexts[0],
        provider_attempt=1,
        lease_owner=lease.owner,
    )
    governance.mark_send_started(permit, lease_owner=lease.owner)
    with postgres_store() as session, session.begin():
        run = session.get(EvaluationRun, lease.run_id)
        assert run is not None
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    barrier = Barrier(2)

    def settle_actual() -> None:
        barrier.wait(timeout=5)
        governance.finish(
            permit,
            disposition=ProviderAttemptDisposition.SETTLED_ACTUAL,
            outcome=ProviderAttemptOutcome.SUCCEEDED,
            input_tokens=3,
            output_tokens=1,
            actual_cost_usd=Decimal("0.05000000"),
        )

    def reconcile() -> tuple[int, int]:
        barrier.wait(timeout=5)
        return governance.reconcile_run_lease(run_id=lease.run_id, lease_token=lease.token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        settle_future = executor.submit(settle_actual)
        reconcile_future = executor.submit(reconcile)
        settle_future.result(timeout=10)
        reconcile_result = reconcile_future.result(timeout=10)

    assert reconcile_result in {(0, 0), (0, 1)}
    governance.finish(
        permit,
        disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
        outcome=ProviderAttemptOutcome.UNEXPECTED_ERROR,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )
    assert governance.reconcile_run_lease(
        run_id=lease.run_id,
        lease_token=lease.token,
    ) == (0, 0)

    with postgres_store() as session:
        reservation = session.get(ProviderCallReservation, permit.reservation_id)
        assert reservation is not None
        if reconcile_result == (0, 1):
            assert reservation.state == ProviderCallReservationState.SETTLED_CONSERVATIVE
            assert reservation.actual_input_tokens == 4
            assert reservation.actual_output_tokens == 2
            assert reservation.actual_cost_usd == Decimal("0.10000000")
            assert reservation.outcome_code == "lease_reconciled_unknown"
            expected_input = 4
            expected_output = 2
            expected_cost = Decimal("0.10000000")
            expected_reconciled = True
        else:
            assert reservation.state == ProviderCallReservationState.SETTLED_ACTUAL
            assert reservation.actual_input_tokens == 3
            assert reservation.actual_output_tokens == 1
            assert reservation.actual_cost_usd == Decimal("0.05000000")
            assert reservation.outcome_code == ProviderAttemptOutcome.SUCCEEDED.value
            expected_input = 3
            expected_output = 1
            expected_cost = Decimal("0.05000000")
            expected_reconciled = False

        scopes = list(session.scalars(select(GovernanceScope)))
        assert len(scopes) == 4
        assert all(scope.active_reservations == 0 for scope in scopes)
        assert all(scope.reserved_requests == 0 for scope in scopes)
        assert all(scope.reserved_input_tokens == 0 for scope in scopes)
        assert all(scope.reserved_output_tokens == 0 for scope in scopes)
        assert all(scope.reserved_cost_usd == 0 for scope in scopes)
        assert all(scope.consumed_requests == 1 for scope in scopes)
        assert all(scope.consumed_input_tokens == expected_input for scope in scopes)
        assert all(scope.consumed_output_tokens == expected_output for scope in scopes)
        assert all(scope.consumed_cost_usd == expected_cost for scope in scopes)

        buckets = list(session.scalars(select(GovernanceMinuteBucket)))
        assert len(buckets) == 4
        assert all(bucket.reserved_requests == 0 for bucket in buckets)
        assert all(bucket.reserved_input_tokens == 0 for bucket in buckets)
        assert all(bucket.reserved_output_tokens == 0 for bucket in buckets)
        assert all(bucket.consumed_requests == 1 for bucket in buckets)
        assert all(bucket.consumed_input_tokens == expected_input for bucket in buckets)
        assert all(bucket.consumed_output_tokens == expected_output for bucket in buckets)

        settlement_events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_key == f"reservation:{permit.reservation_id}:settled"
                )
            )
        )
        assert len(settlement_events) == 1
        assert settlement_events[0].payload["reconciled"] is expected_reconciled
        assert settlement_events[0].payload["input_tokens"] == expected_input
        assert settlement_events[0].payload["output_tokens"] == expected_output


def test_postgres_concurrent_audit_replay_is_idempotent_and_conflicts_fail_closed(
    postgres_store,
) -> None:
    occurred_at = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

    def append(result: str, barrier: Barrier, event_key: str) -> str:
        with postgres_store() as session, session.begin():
            barrier.wait(timeout=5)
            return append_audit_event(
                session,
                event_key=event_key,
                event_type="queue_notification",
                occurred_at=occurred_at,
                payload={"result": result},
                run_id="run-audit-pg",
            ).id

    same_barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        same_futures = [
            executor.submit(
                append,
                "published",
                same_barrier,
                "queue:postgres:concurrent:same",
            )
            for _ in range(2)
        ]
        same_ids = {future.result(timeout=10) for future in same_futures}
    assert len(same_ids) == 1

    conflict_barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        conflict_futures = [
            executor.submit(
                append,
                result,
                conflict_barrier,
                "queue:postgres:concurrent:conflict",
            )
            for result in ("published", "unavailable")
        ]
        conflict_ids: list[str] = []
        conflict_errors: list[Exception] = []
        for future in conflict_futures:
            try:
                conflict_ids.append(future.result(timeout=10))
            except Exception as error:
                conflict_errors.append(error)

    assert len(conflict_ids) == 1
    assert len(conflict_errors) == 1
    assert isinstance(conflict_errors[0], AuditIntegrityError)
    with postgres_store() as session:
        same_events = list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.event_key == "queue:postgres:concurrent:same")
            )
        )
        conflict_events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_key == "queue:postgres:concurrent:conflict"
                )
            )
        )
        assert len(same_events) == 1
        assert same_events[0].id in same_ids
        assert same_events[0].payload == {"result": "published"}
        assert len(conflict_events) == 1
        assert conflict_events[0].id == conflict_ids[0]
        assert conflict_events[0].payload["result"] in {"published", "unavailable"}


def test_postgres_model_lock_serializes_run_creation_before_sensitive_patch(
    postgres_store,
) -> None:
    model_id = "model-pg-credential-lock"
    run_id = "run-pg-credential-lock"
    with postgres_store() as session, session.begin():
        session.add(
            Model(
                id=model_id,
                name="Postgres Credential Lock",
                provider_type=ProviderType.OPENAI_COMPATIBLE,
                base_url="https://provider.example/v1",
                remote_model_name="provider-model",
                api_key_env="PROVIDER_KEY",
                credential_source=CredentialSource.ENVIRONMENT,
            )
        )

    creator_has_lock = Event()
    updater_entered = Event()

    def create_pending_run() -> str:
        with postgres_store() as session, session.begin():
            model = session.scalar(select(Model).where(Model.id == model_id).with_for_update())
            assert model is not None
            creator_has_lock.set()
            assert updater_entered.wait(timeout=5)
            # Let PostgreSQL receive the updater's SELECT FOR UPDATE before this
            # transaction inserts and commits the pending Run.
            time.sleep(0.1)
            session.add(
                EvaluationRun(
                    id=run_id,
                    model_id=model_id,
                    benchmark_id="benchmark-pg",
                    status=RunStatus.PENDING,
                    model_parameters_snapshot={},
                    benchmark_hash_snapshot="postgres-lease-hash",
                    prompt_template_snapshot={},
                    total_questions=1,
                )
            )
        return "created"

    def patch_provider_origin() -> str:
        assert creator_has_lock.wait(timeout=5)
        with postgres_store() as session, session.begin():
            updater_entered.set()
            model = session.scalar(select(Model).where(Model.id == model_id).with_for_update())
            assert model is not None
            if _active_run_exists(session, model_id):
                return "blocked"
            model.base_url = "https://attacker.example/v1"
            return "updated"

    with ThreadPoolExecutor(max_workers=2) as executor:
        create_future = executor.submit(create_pending_run)
        update_future = executor.submit(patch_provider_origin)
        assert create_future.result(timeout=10) == "created"
        assert update_future.result(timeout=10) == "blocked"

    with postgres_store() as session:
        model = session.get(Model, model_id)
        run = session.get(EvaluationRun, run_id)
        assert model is not None and model.base_url == "https://provider.example/v1"
        assert run is not None and run.status == RunStatus.PENDING
