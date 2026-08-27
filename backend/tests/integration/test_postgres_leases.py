"""Real PostgreSQL lease and fencing evidence; skipped without an explicit test DSN."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1.models import _active_run_exists
from app.db.session import create_database_engine
from app.models import (
    Benchmark,
    CredentialSource,
    EvaluationResponse,
    EvaluationRun,
    Model,
    ProviderType,
    Question,
    RunStatus,
)
from app.runners.run_leases import CancelDisposition, ResponseDisposition, RunLeaseRepository

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
                "TRUNCATE evaluation_responses, evaluation_runs, questions, benchmarks, models "
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
