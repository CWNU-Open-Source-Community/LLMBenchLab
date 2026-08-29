"""Database governance reservation, reconciliation, and audit invariants."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import (
    ProviderAttemptDisposition,
    ProviderAttemptOutcome,
)
from app.db.base import Base
from app.db.session import create_database_engine
from app.governance import (
    AuditIntegrityError,
    GovernanceBacklogFull,
    GovernanceDeferred,
    GovernanceExhausted,
    GovernanceFenceLost,
    GovernanceIntegrityError,
    GovernanceRepository,
    append_audit_event,
    provider_scope_key,
)
from app.models import (
    AuditEvent,
    Benchmark,
    EvaluationRun,
    GovernanceMinuteBucket,
    GovernancePolicy,
    GovernanceRunStatus,
    GovernanceScope,
    Model,
    ProviderCallReservation,
    ProviderCallReservationState,
    Question,
    QuestionExecution,
    RunStatus,
)
from app.runners.run_leases import RunLease, RunLeaseRepository


@dataclass
class FixedDatabaseClock:
    current: datetime

    def __call__(self, _session: Session) -> datetime:
        return self.current

    def advance(self, **changes: float) -> None:
        self.current += timedelta(**changes)


@pytest.fixture
def governance_store(tmp_path: Path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'governance.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )
    with factory() as session, session.begin():
        model = Model(id="model-governance", name="Governance Mock", provider_type="mock")
        benchmark = Benchmark(
            id="benchmark-governance",
            slug="governance-fixture",
            name="Governance fixture",
            version="1.0.0",
            description="fixture",
            dimension="general",
            language="en",
            license="MIT",
            source="local",
            evaluator_type="exact_match",
            evaluator_config={},
            prompt_template={},
            dataset_hash="governance-hash",
            question_count=2,
        )
        session.add_all(
            [
                model,
                benchmark,
                Question(
                    id="question-governance-1",
                    benchmark_id=benchmark.id,
                    external_id="q1",
                    position=0,
                    question_type="exact_match",
                    prompt="One?",
                    reference_answer="one",
                ),
                Question(
                    id="question-governance-2",
                    benchmark_id=benchmark.id,
                    external_id="q2",
                    position=1,
                    question_type="exact_match",
                    prompt="Two?",
                    reference_answer="two",
                ),
            ]
        )
    try:
        yield factory
    finally:
        engine.dispose()


def _pending_run(run_id: str, *, total_questions: int = 2) -> EvaluationRun:
    return EvaluationRun(
        id=run_id,
        model_id="model-governance",
        benchmark_id="benchmark-governance",
        status=RunStatus.PENDING,
        model_parameters_snapshot={
            "execution": {"retry_policy": {"max_attempts": 3}},
        },
        benchmark_hash_snapshot="governance-hash",
        prompt_template_snapshot={},
        total_questions=total_questions,
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


def _admit(
    factory: sessionmaker[Session],
    repository: GovernanceRepository,
    run_id: str = "run-governance",
) -> None:
    with factory() as session, session.begin():
        repository.admit_run(
            session,
            _pending_run(run_id),
            provider_type="mock",
            base_url=None,
        )


def _claim(
    factory: sessionmaker[Session],
    clock: FixedDatabaseClock,
    run_id: str = "run-governance",
    owner: str = "worker-governance",
) -> RunLease:
    lease = RunLeaseRepository(
        factory,
        lease_for=timedelta(seconds=30),
        clock=clock,
    ).claim(run_id, owner=owner)
    assert lease is not None
    return lease


def _context(
    repository: GovernanceRepository,
    lease: RunLease,
    *,
    question_id: str = "question-governance-1",
):
    return repository.question_context(
        run_id=lease.run_id,
        question_id=question_id,
        model_id="model-governance",
        provider_scope=provider_scope_key("mock", None),
        lease_owner=lease.owner,
        lease_token=lease.token,
        reserved_output_tokens=2,
        reserved_cost_usd=Decimal("0.00001000"),
    )


def test_provider_scope_is_stable_and_does_not_expose_origin() -> None:
    first = provider_scope_key("openai_compatible", "https://Provider.Example/v1")
    second = provider_scope_key("openai_compatible", "https://provider.example:443/other")

    assert first == second
    assert len(first) == 64
    assert "provider" not in first
    assert provider_scope_key("mock", None) != first
    assert provider_scope_key(
        "openai_compatible", "https://[2001:0db8::1]:443/v1"
    ) == provider_scope_key("openai_compatible", "https://[2001:db8::1]/other")
    assert provider_scope_key(
        "openai_compatible", "https://bücher.example/v1"
    ) == provider_scope_key("openai_compatible", "https://xn--bcher-kva.example/other")


def test_sqlite_database_rejects_a_second_active_policy(governance_store) -> None:
    now = datetime(2026, 8, 27, 14, 0, tzinfo=UTC)
    with governance_store() as session, session.begin():
        session.add(
            GovernancePolicy(
                id="policy-direct-a",
                version=1,
                policy_hash="a" * 64,
                is_active=True,
                activated_at=now,
                created_at=now,
                **_full_policy(),
            )
        )

    with pytest.raises(IntegrityError), governance_store() as session, session.begin():
        session.add(
            GovernancePolicy(
                id="policy-direct-b",
                version=2,
                policy_hash="b" * 64,
                is_active=True,
                activated_at=now,
                created_at=now,
                **_full_policy(backlog_limit=999),
            )
        )

    with governance_store() as session:
        assert (
            session.scalar(
                select(func.count(GovernancePolicy.id)).where(GovernancePolicy.is_active.is_(True))
            )
            == 1
        )


def test_sqlite_policy_writes_begin_immediate_before_first_query(governance_store) -> None:
    repository = GovernanceRepository(
        governance_store,
        clock=FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC)),
    )
    engine = governance_store.kw["bind"]
    statements: list[str] = []

    def record_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        with governance_store() as session, session.begin():
            repository.ensure_default_policy(session)
        assert statements[0].strip().upper() == "BEGIN IMMEDIATE"

        statements.clear()
        with governance_store() as session, session.begin():
            repository.apply_policy(
                session,
                _full_policy(backlog_limit=111, question_quantum=7),
            )
        assert statements[0].strip().upper() == "BEGIN IMMEDIATE"
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)


def test_policy_apply_can_reactivate_a_previous_revision(governance_store) -> None:
    repository = GovernanceRepository(
        governance_store,
        clock=FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC)),
    )
    policy_a = _full_policy(backlog_limit=111, question_quantum=7)
    policy_b = _full_policy(backlog_limit=222, question_quantum=9)

    applied: list[tuple[str, int]] = []
    for values in (policy_a, policy_b, policy_a):
        with governance_store() as session, session.begin():
            policy = repository.apply_policy(session, values)
            applied.append((policy.id, policy.version))

    assert applied[0] == applied[2]
    assert applied[0] != applied[1]
    with governance_store() as session:
        policies = list(
            session.scalars(select(GovernancePolicy).order_by(GovernancePolicy.version))
        )
        assert [policy.version for policy in policies] == [1, 2, 3]
        assert [policy.id for policy in policies if policy.is_active] == [applied[0][0]]


def test_concurrent_sqlite_policy_bootstrap_is_idempotent(governance_store) -> None:
    repository = GovernanceRepository(
        governance_store,
        clock=FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC)),
    )
    barrier = Barrier(2)

    def bootstrap() -> str:
        with governance_store() as session, session.begin():
            barrier.wait(timeout=5)
            return repository.ensure_default_policy(session).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(bootstrap) for _ in range(2)]
        policy_ids = [future.result(timeout=10) for future in futures]

    assert len(set(policy_ids)) == 1
    with governance_store() as session:
        assert session.scalar(select(func.count(GovernancePolicy.id))) == 1
        assert (
            session.scalar(
                select(func.count(GovernancePolicy.id)).where(GovernancePolicy.is_active.is_(True))
            )
            == 1
        )


def test_concurrent_sqlite_policy_apply_serializes_versions(governance_store) -> None:
    repository = GovernanceRepository(
        governance_store,
        clock=FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC)),
    )
    with governance_store() as session, session.begin():
        repository.ensure_default_policy(session)
    barrier = Barrier(2)
    policies = (
        _full_policy(backlog_limit=111, question_quantum=7),
        _full_policy(backlog_limit=222, question_quantum=9),
    )

    def apply(values: dict[str, object]) -> tuple[str, int]:
        with governance_store() as session, session.begin():
            barrier.wait(timeout=5)
            policy = repository.apply_policy(session, values)
            return policy.id, policy.version

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply, values) for values in policies]
        applied = [future.result(timeout=10) for future in futures]

    assert {version for _policy_id, version in applied} == {2, 3}
    with governance_store() as session:
        assert session.scalar(select(func.count(GovernancePolicy.id))) == 3
        assert (
            session.scalar(
                select(func.count(GovernancePolicy.id)).where(GovernancePolicy.is_active.is_(True))
            )
            == 1
        )


def test_concurrent_sqlite_policy_reactivation_is_idempotent(governance_store) -> None:
    repository = GovernanceRepository(
        governance_store,
        clock=FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC)),
    )
    policy_a = _full_policy(backlog_limit=111, question_quantum=7)
    policy_b = _full_policy(backlog_limit=222, question_quantum=9)
    with governance_store() as session, session.begin():
        target_id = repository.apply_policy(session, policy_a).id
    with governance_store() as session, session.begin():
        repository.apply_policy(session, policy_b)
    barrier = Barrier(2)

    def reactivate() -> str:
        with governance_store() as session, session.begin():
            barrier.wait(timeout=5)
            return repository.apply_policy(session, policy_a).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reactivate) for _ in range(2)]
        reactivated_ids = [future.result(timeout=10) for future in futures]

    assert reactivated_ids == [target_id, target_id]
    with governance_store() as session:
        active = list(
            session.scalars(select(GovernancePolicy).where(GovernancePolicy.is_active.is_(True)))
        )
        assert [policy.id for policy in active] == [target_id]
        assert session.scalar(select(func.count(GovernancePolicy.id))) == 3


def test_run_admission_bootstraps_and_freezes_policy(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    _admit(governance_store, repository)

    with governance_store() as session:
        run = session.get(EvaluationRun, "run-governance")
        policy = session.scalar(select(GovernancePolicy))
        scope = session.scalar(select(GovernanceScope).where(GovernanceScope.scope_key == "global"))
        assert run is not None and policy is not None and scope is not None
        assert run.governance_status == GovernanceRunStatus.MANAGED
        assert run.governance_policy_id == policy.id
        governance = run.model_parameters_snapshot["governance"]
        assert governance["policy_hash"] == policy.policy_hash
        assert governance["provider_scope_key"] == provider_scope_key("mock", None)
        assert "http" not in str(governance).lower()
        assert session.scalar(select(func.count(AuditEvent.id))) == 2

    with governance_store() as session, session.begin():
        repository.apply_policy(session, _full_policy(backlog_limit=1))
    with governance_store() as session, session.begin():
        with pytest.raises(GovernanceBacklogFull) as caught:
            repository.admit_run(
                session,
                _pending_run("run-governance-second"),
                provider_type="mock",
                base_url=None,
            )
        assert caught.value.limit == 1


def test_attempt_actual_settlement_updates_all_scopes_and_bucket(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)
    context = _context(repository, lease)

    permit = repository.reserve(
        context,
        provider_attempt=1,
        lease_owner=lease.owner,
    )
    repository.mark_send_started(permit, lease_owner=lease.owner)
    repository.finish(
        permit,
        disposition=ProviderAttemptDisposition.SETTLED_ACTUAL,
        outcome=ProviderAttemptOutcome.SUCCEEDED,
        input_tokens=3,
        output_tokens=2,
        actual_cost_usd=Decimal("0.00000800"),
    )

    with governance_store() as session:
        reservation = session.get(ProviderCallReservation, permit.reservation_id)
        assert reservation is not None
        assert reservation.state == ProviderCallReservationState.SETTLED_ACTUAL
        assert reservation.actual_input_tokens == 3
        assert reservation.actual_output_tokens == 2
        assert reservation.actual_cost_usd == Decimal("0.00000800")
        scopes = list(session.scalars(select(GovernanceScope)))
        assert len(scopes) == 4
        assert all(scope.active_reservations == 0 for scope in scopes)
        assert all(scope.reserved_requests == 0 for scope in scopes)
        assert all(scope.reserved_input_tokens == 0 for scope in scopes)
        assert all(scope.reserved_output_tokens == 0 for scope in scopes)
        assert all(scope.consumed_requests == 1 for scope in scopes)
        assert all(scope.consumed_input_tokens == 3 for scope in scopes)
        assert all(scope.consumed_output_tokens == 2 for scope in scopes)
        assert all(scope.consumed_cost_usd == Decimal("0.00000800") for scope in scopes)
        assert all(scope.overdrawn is False for scope in scopes)
        buckets = list(session.scalars(select(GovernanceMinuteBucket)))
        assert len(buckets) == 4
        assert all(bucket.consumed_requests == 1 for bucket in buckets)
        assert all(bucket.consumed_input_tokens == 3 for bucket in buckets)
        execution = session.scalar(select(QuestionExecution))
        assert execution is not None and execution.next_provider_attempt == 2
        event_types = list(
            session.scalars(select(AuditEvent.event_type).order_by(AuditEvent.occurred_at))
        )
        assert event_types.count("provider_attempt_reserved") == 1
        assert event_types.count("provider_attempt_send_started") == 1
        assert event_types.count("provider_attempt_settled") == 1

    # Terminal settlement is a first-writer-wins idempotent no-op.
    repository.finish(
        permit,
        disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
        outcome=ProviderAttemptOutcome.UNEXPECTED_ERROR,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )
    with governance_store() as session:
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.event_type == "provider_attempt_settled"
                )
            )
            == 1
        )


def test_observational_input_usage_does_not_create_hard_overdraw(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)
    context = repository.question_context(
        run_id=lease.run_id,
        question_id="question-governance-1",
        model_id="model-governance",
        provider_scope=provider_scope_key("mock", None),
        lease_owner=lease.owner,
        lease_token=lease.token,
        reserved_output_tokens=None,
        reserved_cost_usd=Decimal("0.00010000"),
    )

    assert context.reserved_input_tokens is None
    assert context.reserved_cost_usd is None
    permit = repository.reserve(context, provider_attempt=1, lease_owner=lease.owner)
    repository.mark_send_started(permit, lease_owner=lease.owner)
    repository.finish(
        permit,
        disposition=ProviderAttemptDisposition.SETTLED_ACTUAL,
        outcome=ProviderAttemptOutcome.SUCCEEDED,
        input_tokens=75,
        output_tokens=5,
        actual_cost_usd=Decimal("0.00020000"),
    )

    with governance_store() as session:
        reservation = session.get(ProviderCallReservation, permit.reservation_id)
        assert reservation is not None
        assert reservation.reserved_input_tokens is None
        assert reservation.reserved_cost_usd is None
        assert reservation.actual_input_tokens == 75
        assert reservation.actual_cost_usd == Decimal("0.00020000")
        assert all(scope.overdrawn is False for scope in session.scalars(select(GovernanceScope)))

    next_context = repository.question_context(
        run_id=lease.run_id,
        question_id="question-governance-2",
        model_id="model-governance",
        provider_scope=provider_scope_key("mock", None),
        lease_owner=lease.owner,
        lease_token=lease.token,
        reserved_output_tokens=None,
        reserved_cost_usd=None,
    )
    next_permit = repository.reserve(
        next_context,
        provider_attempt=1,
        lease_owner=lease.owner,
    )
    repository.finish(
        next_permit,
        disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
        outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )


def test_explicit_input_reservation_overdraw_still_stops_following_attempts(
    governance_store,
) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    run = _pending_run("run-explicit-input-overdraw")
    run.input_token_reservation = 59
    with governance_store() as session, session.begin():
        repository.admit_run(session, run, provider_type="mock", base_url=None)
    lease = _claim(
        governance_store,
        clock,
        run_id=run.id,
        owner="worker-explicit-input-overdraw",
    )
    context = repository.question_context(
        run_id=lease.run_id,
        question_id="question-governance-1",
        model_id="model-governance",
        provider_scope=provider_scope_key("mock", None),
        lease_owner=lease.owner,
        lease_token=lease.token,
        reserved_output_tokens=None,
        reserved_cost_usd=None,
    )
    permit = repository.reserve(context, provider_attempt=1, lease_owner=lease.owner)
    repository.mark_send_started(permit, lease_owner=lease.owner)
    repository.finish(
        permit,
        disposition=ProviderAttemptDisposition.SETTLED_ACTUAL,
        outcome=ProviderAttemptOutcome.SUCCEEDED,
        input_tokens=75,
        output_tokens=5,
        actual_cost_usd=None,
    )

    with governance_store() as session:
        assert all(scope.overdrawn is True for scope in session.scalars(select(GovernanceScope)))

    next_context = repository.question_context(
        run_id=lease.run_id,
        question_id="question-governance-2",
        model_id="model-governance",
        provider_scope=provider_scope_key("mock", None),
        lease_owner=lease.owner,
        lease_token=lease.token,
        reserved_output_tokens=None,
        reserved_cost_usd=None,
    )
    with pytest.raises(GovernanceExhausted) as caught:
        repository.reserve(next_context, provider_attempt=1, lease_owner=lease.owner)
    assert caught.value.code == "governance_global_overdrawn"


def test_explicit_output_reservation_overdraw_remains_hard(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)
    context = repository.question_context(
        run_id=lease.run_id,
        question_id="question-governance-1",
        model_id="model-governance",
        provider_scope=provider_scope_key("mock", None),
        lease_owner=lease.owner,
        lease_token=lease.token,
        reserved_output_tokens=4,
        reserved_cost_usd=None,
    )
    permit = repository.reserve(context, provider_attempt=1, lease_owner=lease.owner)
    repository.mark_send_started(permit, lease_owner=lease.owner)
    repository.finish(
        permit,
        disposition=ProviderAttemptDisposition.SETTLED_ACTUAL,
        outcome=ProviderAttemptOutcome.SUCCEEDED,
        input_tokens=1,
        output_tokens=5,
        actual_cost_usd=None,
    )

    with governance_store() as session:
        assert all(scope.overdrawn is True for scope in session.scalars(select(GovernanceScope)))


def test_explicit_cost_reservation_overdraw_remains_hard(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    run = _pending_run("run-explicit-cost-overdraw")
    run.input_token_reservation = 10
    with governance_store() as session, session.begin():
        repository.admit_run(session, run, provider_type="mock", base_url=None)
    lease = _claim(
        governance_store,
        clock,
        run_id=run.id,
        owner="worker-explicit-cost-overdraw",
    )
    context = repository.question_context(
        run_id=lease.run_id,
        question_id="question-governance-1",
        model_id="model-governance",
        provider_scope=provider_scope_key("mock", None),
        lease_owner=lease.owner,
        lease_token=lease.token,
        reserved_output_tokens=10,
        reserved_cost_usd=Decimal("0.00010000"),
    )
    permit = repository.reserve(context, provider_attempt=1, lease_owner=lease.owner)
    repository.mark_send_started(permit, lease_owner=lease.owner)
    repository.finish(
        permit,
        disposition=ProviderAttemptDisposition.SETTLED_ACTUAL,
        outcome=ProviderAttemptOutcome.SUCCEEDED,
        input_tokens=5,
        output_tokens=5,
        actual_cost_usd=Decimal("0.00020000"),
    )

    with governance_store() as session:
        assert all(scope.overdrawn is True for scope in session.scalars(select(GovernanceScope)))


def test_pre_send_release_and_expired_send_reconciliation(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    _admit(governance_store, repository)
    first_lease = _claim(governance_store, clock)
    first_context = _context(repository, first_lease)
    first = repository.reserve(
        first_context,
        provider_attempt=1,
        lease_owner=first_lease.owner,
    )
    repository.finish(
        first,
        disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
        outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )
    repository.finish(
        first,
        disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
        outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )

    second_context = _context(repository, first_lease)
    assert second_context.execution_generation == 1
    assert second_context.next_provider_attempt == 1
    second = repository.reserve(
        second_context,
        provider_attempt=1,
        lease_owner=first_lease.owner,
    )
    repository.mark_send_started(second, lease_owner=first_lease.owner)
    clock.advance(seconds=31)
    with pytest.raises(GovernanceFenceLost) as caught:
        repository.mark_send_started(second, lease_owner=first_lease.owner)
    assert caught.value.code == "governance_lease_fence_lost"
    assert repository.reconcile_run_lease(run_id=first_lease.run_id, lease_token=1) == (0, 1)
    assert repository.reconcile_run_lease(run_id=first_lease.run_id, lease_token=1) == (0, 0)

    with governance_store() as session:
        first_row = session.get(ProviderCallReservation, first.reservation_id)
        second_row = session.get(ProviderCallReservation, second.reservation_id)
        assert first_row is not None and second_row is not None
        assert first_row.state == ProviderCallReservationState.RELEASED_PRE_SEND
        assert second_row.state == ProviderCallReservationState.SETTLED_CONSERVATIVE
        assert second_row.actual_input_tokens is None
        assert second_row.actual_output_tokens == 2
        assert second_row.actual_cost_usd is None
        scopes = list(session.scalars(select(GovernanceScope)))
        assert all(scope.active_reservations == 0 for scope in scopes)
        assert all(scope.consumed_requests == 1 for scope in scopes)
        reconciled = list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == "run_lease_reconciled")
            )
        )
        assert len(reconciled) == 1
        assert reconciled[0].payload == {
            "released_reservations": 0,
            "conservative_settlements": 1,
        }


def test_confirmed_pre_send_release_preserves_zero_retry_http_budget(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    run = _pending_run("run-zero-retry")
    run.model_parameters_snapshot["execution"]["retry_policy"]["max_attempts"] = 1
    with governance_store() as session, session.begin():
        repository.admit_run(session, run, provider_type="mock", base_url=None)
    lease = _claim(
        governance_store,
        clock,
        run_id=run.id,
        owner="worker-zero-retry",
    )

    first_context = _context(repository, lease)
    first = repository.reserve(first_context, provider_attempt=1, lease_owner=lease.owner)
    repository.finish(
        first,
        disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
        outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )

    resumed_context = _context(repository, lease)
    assert resumed_context.execution_generation == 1
    assert resumed_context.next_provider_attempt == 1
    resumed = repository.reserve(
        resumed_context,
        provider_attempt=1,
        lease_owner=lease.owner,
    )
    with governance_store() as session:
        rows = list(
            session.scalars(
                select(ProviderCallReservation).order_by(
                    ProviderCallReservation.execution_generation
                )
            )
        )
        assert [row.state for row in rows] == [
            ProviderCallReservationState.RELEASED_PRE_SEND,
            ProviderCallReservationState.RESERVED,
        ]
        assert [row.provider_attempt for row in rows] == [1, 1]
        assert [row.execution_generation for row in rows] == [0, 1]

    repository.finish(
        resumed,
        disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
        outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )


def test_pre_send_release_retries_only_the_current_unsent_ordinal(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)

    first = repository.reserve(
        _context(repository, lease),
        provider_attempt=1,
        lease_owner=lease.owner,
    )
    repository.mark_send_started(first, lease_owner=lease.owner)
    repository.finish(
        first,
        disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
        outcome=ProviderAttemptOutcome.HTTP_ERROR,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )
    second_context = _context(repository, lease)
    assert second_context.next_provider_attempt == 2
    second = repository.reserve(
        second_context,
        provider_attempt=2,
        lease_owner=lease.owner,
    )
    repository.finish(
        second,
        disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
        outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )

    resumed = _context(repository, lease)
    assert resumed.execution_generation == 1
    assert resumed.next_provider_attempt == 2
    with governance_store() as session:
        execution = session.scalar(select(QuestionExecution))
        assert execution is not None
        assert execution.first_attempt_at == clock.current


def test_concurrency_limit_defers_without_partial_reservation(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    with governance_store() as session, session.begin():
        repository.apply_policy(session, _full_policy(global_concurrency_limit=1))
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)
    first_context = _context(repository, lease)
    repository.reserve(first_context, provider_attempt=1, lease_owner=lease.owner)
    second_context = _context(
        repository,
        lease,
        question_id="question-governance-2",
    )

    with pytest.raises(GovernanceDeferred) as caught:
        repository.reserve(second_context, provider_attempt=1, lease_owner=lease.owner)
    assert caught.value.code == "governance_global_concurrency"
    assert caught.value.not_before == clock.current + timedelta(seconds=1)
    with governance_store() as session:
        assert session.scalar(select(func.count(ProviderCallReservation.id))) == 1
        assert all(
            scope.active_reservations == 1 for scope in session.scalars(select(GovernanceScope))
        )


@pytest.mark.parametrize("tampered_active", [0, 2])
def test_scope_counter_drift_fails_closed_before_admission(
    governance_store,
    tampered_active: int,
) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    with governance_store() as session, session.begin():
        repository.apply_policy(session, _full_policy(global_concurrency_limit=1))
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)
    repository.reserve(_context(repository, lease), provider_attempt=1, lease_owner=lease.owner)

    with governance_store() as session, session.begin():
        global_scope = session.scalar(
            select(GovernanceScope).where(GovernanceScope.scope_key == "global")
        )
        assert global_scope is not None
        global_scope.active_reservations = tampered_active

    with pytest.raises(GovernanceIntegrityError) as caught:
        repository.reserve(
            _context(repository, lease, question_id="question-governance-2"),
            provider_attempt=1,
            lease_owner=lease.owner,
        )
    assert caught.value.code == "governance_scope_counter_drift"
    with governance_store() as session:
        assert session.scalar(select(func.count(ProviderCallReservation.id))) == 1


@pytest.mark.parametrize("tampered_reserved", [0, 2])
def test_minute_bucket_counter_drift_fails_closed_before_admission(
    governance_store,
    tampered_reserved: int,
) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)
    repository.reserve(_context(repository, lease), provider_attempt=1, lease_owner=lease.owner)

    with governance_store() as session, session.begin():
        global_scope = session.scalar(
            select(GovernanceScope).where(GovernanceScope.scope_key == "global")
        )
        assert global_scope is not None
        bucket = session.scalar(
            select(GovernanceMinuteBucket).where(GovernanceMinuteBucket.scope_id == global_scope.id)
        )
        assert bucket is not None
        bucket.reserved_requests = tampered_reserved

    with pytest.raises(GovernanceIntegrityError) as caught:
        repository.reserve(
            _context(repository, lease, question_id="question-governance-2"),
            provider_attempt=1,
            lease_owner=lease.owner,
        )
    assert caught.value.code == "governance_minute_bucket_counter_drift"
    with governance_store() as session:
        assert session.scalar(select(func.count(ProviderCallReservation.id))) == 1


@pytest.mark.parametrize(
    ("column", "tampered"),
    [
        ("input_token_reservation", 8),
        ("lifetime_request_budget", 2),
        ("lifetime_token_budget", 12),
        ("lifetime_cost_budget_usd", Decimal("0.00002000")),
    ],
)
def test_run_override_drift_cannot_bypass_frozen_snapshot(
    governance_store,
    column: str,
    tampered: object,
) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    run = _pending_run("run-frozen-overrides")
    run.input_token_reservation = 4
    run.lifetime_request_budget = 1
    run.lifetime_token_budget = 6
    run.lifetime_cost_budget_usd = Decimal("0.00001000")
    with governance_store() as session, session.begin():
        repository.admit_run(session, run, provider_type="mock", base_url=None)
    lease = _claim(
        governance_store,
        clock,
        run_id=run.id,
        owner="worker-frozen-overrides",
    )
    context = _context(repository, lease)

    with governance_store() as session, session.begin():
        stored = session.get(EvaluationRun, run.id)
        assert stored is not None
        setattr(stored, column, tampered)

    with pytest.raises(GovernanceIntegrityError) as caught:
        repository.reserve(context, provider_attempt=1, lease_owner=lease.owner)
    assert caught.value.code == "governance_run_override_snapshot_mismatch"
    with governance_store() as session:
        assert session.scalar(select(func.count(ProviderCallReservation.id))) == 0


def test_policy_column_drift_cannot_bypass_frozen_policy_hash(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    with governance_store() as session, session.begin():
        repository.apply_policy(session, _full_policy(global_concurrency_limit=1))
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)
    context = _context(repository, lease)

    with governance_store() as session, session.begin():
        policy = session.get(
            GovernancePolicy,
            session.get(EvaluationRun, lease.run_id).governance_policy_id,
        )
        assert policy is not None
        policy.global_concurrency_limit = 2

    with pytest.raises(GovernanceIntegrityError) as caught:
        repository.reserve(context, provider_attempt=1, lease_owner=lease.owner)
    assert caught.value.code == "governance_policy_hash_mismatch"


def test_send_start_moves_reservation_to_its_database_minute(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, 59, 900000, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)
    context = _context(repository, lease)
    permit = repository.reserve(context, provider_attempt=1, lease_owner=lease.owner)

    clock.advance(seconds=2)
    repository.mark_send_started(permit, lease_owner=lease.owner)

    with governance_store() as session:
        global_scope = session.scalar(
            select(GovernanceScope).where(GovernanceScope.scope_key == "global")
        )
        assert global_scope is not None
        buckets = list(
            session.scalars(
                select(GovernanceMinuteBucket)
                .where(GovernanceMinuteBucket.scope_id == global_scope.id)
                .order_by(GovernanceMinuteBucket.window_start)
            )
        )
        assert len(buckets) == 2
        old_bucket, send_bucket = buckets
        assert old_bucket.reserved_requests == old_bucket.reserved_input_tokens == 0
        assert old_bucket.reserved_output_tokens == 0
        assert send_bucket.consumed_requests == 1
        assert send_bucket.reserved_input_tokens == 0
        assert send_bucket.reserved_output_tokens == 2
        reservation = session.get(ProviderCallReservation, permit.reservation_id)
        assert reservation is not None
        assert reservation.window_start == datetime(2026, 8, 27, 14, 1, tzinfo=UTC)


def test_send_start_rechecks_new_window_before_http(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, 59, 900000, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    with governance_store() as session, session.begin():
        repository.apply_policy(session, _full_policy(global_requests_per_minute=1))
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)
    old_context = _context(repository, lease)
    old_permit = repository.reserve(old_context, provider_attempt=1, lease_owner=lease.owner)

    clock.advance(seconds=2)
    new_context = _context(repository, lease, question_id="question-governance-2")
    new_permit = repository.reserve(new_context, provider_attempt=1, lease_owner=lease.owner)
    repository.mark_send_started(new_permit, lease_owner=lease.owner)

    with pytest.raises(GovernanceDeferred) as caught:
        repository.mark_send_started(old_permit, lease_owner=lease.owner)
    assert caught.value.code == "governance_global_rpm"
    repository.finish(
        old_permit,
        disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
        outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )
    with governance_store() as session:
        old_row = session.get(ProviderCallReservation, old_permit.reservation_id)
        assert old_row is not None
        assert old_row.state == ProviderCallReservationState.RELEASED_PRE_SEND


def test_cost_only_budget_requires_explicit_finite_token_bounds(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    with governance_store() as session, session.begin():
        repository.apply_policy(
            session,
            _full_policy(global_lifetime_cost_budget_usd=Decimal("1.00000000")),
        )
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)

    estimated_context = _context(repository, lease)
    with pytest.raises(GovernanceExhausted) as caught:
        repository.reserve(
            estimated_context,
            provider_attempt=1,
            lease_owner=lease.owner,
        )
    assert caught.value.code == "governance_input_bound_unknown"

    explicit_run = _pending_run("run-cost-explicit")
    explicit_run.input_token_reservation = 4
    with governance_store() as session, session.begin():
        repository.admit_run(
            session,
            explicit_run,
            provider_type="mock",
            base_url=None,
        )
    explicit_lease = _claim(
        governance_store,
        clock,
        run_id="run-cost-explicit",
        owner="worker-cost-explicit",
    )
    unbounded_output = repository.question_context(
        run_id=explicit_lease.run_id,
        question_id="question-governance-1",
        model_id="model-governance",
        provider_scope=provider_scope_key("mock", None),
        lease_owner=explicit_lease.owner,
        lease_token=explicit_lease.token,
        reserved_output_tokens=None,
        reserved_cost_usd=Decimal("0.00001000"),
    )
    with pytest.raises(GovernanceExhausted) as caught:
        repository.reserve(
            unbounded_output,
            provider_attempt=1,
            lease_owner=explicit_lease.owner,
        )
    assert caught.value.code == "governance_unbounded_output"


def test_unknown_unbounded_usage_stays_null_without_false_overdraw(governance_store) -> None:
    clock = FixedDatabaseClock(datetime(2026, 8, 27, 14, 0, tzinfo=UTC))
    repository = GovernanceRepository(governance_store, clock=clock)
    _admit(governance_store, repository)
    lease = _claim(governance_store, clock)
    context = repository.question_context(
        run_id=lease.run_id,
        question_id="question-governance-1",
        model_id="model-governance",
        provider_scope=provider_scope_key("mock", None),
        lease_owner=lease.owner,
        lease_token=lease.token,
        reserved_output_tokens=None,
        reserved_cost_usd=None,
    )
    permit = repository.reserve(context, provider_attempt=1, lease_owner=lease.owner)
    repository.mark_send_started(permit, lease_owner=lease.owner)
    repository.finish(
        permit,
        disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
        outcome=ProviderAttemptOutcome.USAGE_INCOMPLETE,
        input_tokens=3,
        output_tokens=None,
        actual_cost_usd=None,
    )

    with governance_store() as session:
        reservation = session.get(ProviderCallReservation, permit.reservation_id)
        scopes = list(session.scalars(select(GovernanceScope)))
        assert reservation is not None
        assert reservation.actual_input_tokens == 3
        assert reservation.actual_output_tokens is None
        assert reservation.actual_cost_usd is None
        assert all(scope.consumed_input_tokens == 3 for scope in scopes)
        assert all(scope.consumed_output_tokens == 0 for scope in scopes)
        assert all(scope.overdrawn is False for scope in scopes)

    next_context = repository.question_context(
        run_id=lease.run_id,
        question_id="question-governance-1",
        model_id="model-governance",
        provider_scope=provider_scope_key("mock", None),
        lease_owner=lease.owner,
        lease_token=lease.token,
        reserved_output_tokens=None,
        reserved_cost_usd=None,
    )
    next_permit = repository.reserve(
        next_context,
        provider_attempt=2,
        lease_owner=lease.owner,
    )
    repository.finish(
        next_permit,
        disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
        outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
        input_tokens=None,
        output_tokens=None,
        actual_cost_usd=None,
    )


def test_audit_replay_conflict_and_payload_allowlist(governance_store) -> None:
    occurred_at = datetime(2026, 8, 27, 14, 0, tzinfo=UTC)
    replayed_at = occurred_at + timedelta(minutes=5)
    with governance_store() as session, session.begin():
        first = append_audit_event(
            session,
            event_key="queue:run-1:notification",
            event_type="queue_notification",
            occurred_at=occurred_at,
            payload={"result": "published"},
            run_id="run-1",
            duration_ms=12.5,
        )
        replay = append_audit_event(
            session,
            event_key="queue:run-1:notification",
            event_type="queue_notification",
            occurred_at=replayed_at,
            payload={"result": "published"},
            run_id="run-1",
            duration_ms=12.5,
        )
        assert replay.id == first.id
        assert replay.occurred_at == occurred_at
        assert replay.expires_at == occurred_at + timedelta(days=90)
        with pytest.raises(AuditIntegrityError):
            append_audit_event(
                session,
                event_key="queue:run-1:notification",
                event_type="queue_notification",
                occurred_at=occurred_at,
                payload={"result": "unavailable"},
                run_id="run-1",
                duration_ms=12.5,
            )
        with pytest.raises(AuditIntegrityError):
            append_audit_event(
                session,
                event_key="queue:run-1:notification",
                event_type="queue_notification",
                occurred_at=occurred_at,
                payload={"result": "published"},
                run_id="different-run",
                duration_ms=12.5,
            )
        with pytest.raises(AuditIntegrityError):
            append_audit_event(
                session,
                event_key="queue:run-1:notification",
                event_type="queue_notification",
                occurred_at=replayed_at,
                payload={"result": "published"},
                run_id="run-1",
                duration_ms=13.0,
            )
        with pytest.raises(ValueError):
            append_audit_event(
                session,
                event_key="queue:run-2:notification",
                event_type="queue_notification",
                occurred_at=occurred_at,
                payload={"result": "sk-marker-credential"},
                run_id="run-2",
            )
        with pytest.raises(ValueError):
            append_audit_event(
                session,
                event_key="policy:missing-required-fields",
                event_type="governance_policy_applied",
                occurred_at=occurred_at,
                payload={},
            )
        with pytest.raises(ValueError):
            append_audit_event(
                session,
                event_key="policy:null-required-field",
                event_type="governance_policy_applied",
                occurred_at=occurred_at,
                payload={"policy_version": None, "policy_hash": "a" * 64},
            )
        with pytest.raises(ValueError):
            append_audit_event(
                session,
                event_key="credential:secret-shaped-key-id",
                event_type="credential_changed",
                occurred_at=occurred_at,
                payload={
                    "action": "created",
                    "credential_source": "stored",
                    "key_id": "sk-audit-secret-marker-12345678",
                },
            )
        with pytest.raises(ValueError):
            append_audit_event(
                session,
                event_key="credential:url-shaped-key-id",
                event_type="credential_changed",
                occurred_at=occurred_at,
                payload={
                    "action": "created",
                    "credential_source": "stored",
                    "key_id": "https://provider.example/key",
                },
            )
        with pytest.raises(ValueError):
            append_audit_event(
                session,
                event_key="queue:secret-shaped-worker",
                event_type="queue_notification",
                occurred_at=occurred_at,
                payload={"result": "published"},
                run_id="run-2",
                worker_id="sk-audit-worker-secret-12345678",
            )


def test_concurrent_audit_append_is_idempotent_and_conflicts_fail_closed(
    governance_store,
) -> None:
    occurred_at = datetime(2026, 8, 27, 14, 0, tzinfo=UTC)

    def append(result: str, barrier: Barrier, event_key: str) -> str:
        with governance_store() as session, session.begin():
            barrier.wait()
            return append_audit_event(
                session,
                event_key=event_key,
                event_type="queue_notification",
                occurred_at=occurred_at,
                payload={"result": result},
                run_id="run-concurrent",
            ).id

    same_barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        same_futures = [
            executor.submit(append, "published", same_barrier, "queue:concurrent:same")
            for _ in range(2)
        ]
        assert len({future.result() for future in same_futures}) == 1

    conflict_barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        conflict_futures = [
            executor.submit(
                append,
                result,
                conflict_barrier,
                "queue:concurrent:conflict",
            )
            for result in ("published", "unavailable")
        ]
        results: list[str] = []
        errors: list[Exception] = []
        for future in conflict_futures:
            try:
                results.append(future.result())
            except Exception as error:
                errors.append(error)
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], AuditIntegrityError)
