"""Concurrency coverage for cross-database Model configuration locking."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.model_lock import lock_model_for_update
from app.db.session import create_database_engine
from app.models import Benchmark, EvaluationRun, Model, RunStatus


def test_sqlite_model_lock_serializes_run_creation_and_sensitive_update(tmp_path) -> None:
    database_path = tmp_path / "model-lock.db"
    database_engine = create_database_engine(f"sqlite:///{database_path}")
    factory = sessionmaker(
        bind=database_engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )
    Base.metadata.create_all(database_engine)
    with factory() as session, session.begin():
        session.add_all(
            [
                Model(
                    id="model-lock",
                    name="SQLite Model Lock",
                    provider_type="openai_compatible",
                    base_url="https://provider.example/v1",
                    remote_model_name="provider-model",
                    api_key_env="PROVIDER_KEY",
                    credential_source="environment",
                ),
                Benchmark(
                    id="benchmark-lock",
                    slug="sqlite-model-lock",
                    name="SQLite model lock fixture",
                    version="1.0.0",
                    description="fixture",
                    dimension="general",
                    language="en",
                    license="MIT",
                    source="local",
                    evaluator_type="exact_match",
                    evaluator_config={},
                    prompt_template={},
                    dataset_hash="sqlite-model-lock-hash",
                    question_count=1,
                ),
            ]
        )

    creator_has_lock = Event()
    updater_is_waiting = Event()
    updater_has_lock = Event()
    release_creator = Event()

    def create_pending_run() -> str:
        with factory() as session, session.begin():
            model = lock_model_for_update(session, "model-lock")
            assert model is not None
            creator_has_lock.set()
            assert updater_is_waiting.wait(timeout=5)
            assert not updater_has_lock.wait(timeout=0.2)
            session.add(
                EvaluationRun(
                    id="run-lock",
                    model_id=model.id,
                    benchmark_id="benchmark-lock",
                    status=RunStatus.PENDING,
                    model_parameters_snapshot={"model": {"base_url": model.base_url}},
                    benchmark_hash_snapshot="sqlite-model-lock-hash",
                    prompt_template_snapshot={},
                    total_questions=1,
                )
            )
            release_creator.set()
        return "created"

    def update_provider_origin() -> str:
        assert creator_has_lock.wait(timeout=5)
        updater_is_waiting.set()
        with factory() as session, session.begin():
            model = lock_model_for_update(session, "model-lock")
            updater_has_lock.set()
            assert release_creator.is_set()
            assert model is not None
            active_run = session.scalar(
                select(EvaluationRun.id)
                .where(
                    EvaluationRun.model_id == model.id,
                    EvaluationRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
                )
                .limit(1)
            )
            if active_run is not None:
                return "blocked"
            model.base_url = "https://attacker.example/v1"
            return "updated"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            create_future = executor.submit(create_pending_run)
            update_future = executor.submit(update_provider_origin)
            assert create_future.result(timeout=10) == "created"
            assert update_future.result(timeout=10) == "blocked"

        with factory() as session:
            model = session.get(Model, "model-lock")
            run = session.get(EvaluationRun, "run-lock")
            assert model is not None
            assert model.base_url == "https://provider.example/v1"
            assert run is not None
            assert run.model_parameters_snapshot["model"]["base_url"] == model.base_url
    finally:
        database_engine.dispose()
