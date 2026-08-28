"""Worker generation registration, coalescing, and stale read-model tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import create_database_engine
from app.models import WorkerProcess
from app.worker_progress import (
    WorkerProgressEvent,
    WorkerProgressRecorder,
    WorkerProgressStateError,
    _PendingEvent,
    collect_worker_progress,
)


@pytest.fixture
def worker_store(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'worker-progress.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_recorder_uses_one_db_timestamp_for_coalesced_events_and_zero_idle_writes(
    worker_store,
) -> None:
    recorder = WorkerProgressRecorder(
        worker_store,
        worker_id="worker-progress-test",
        generation_id="00000000-0000-0000-0000-000000000001",
        flush_seconds=60,
    )

    await recorder.start()
    with worker_store() as session:
        registered = session.get(WorkerProcess, recorder.generation_id)
        assert registered is not None
        registered_seen_at = registered.last_seen_at
        assert registered.started_at == registered_seen_at
        assert registered.stopped_at is None

    assert await recorder.flush_now() is False
    with worker_store() as session:
        row = session.get(WorkerProcess, recorder.generation_id)
        assert row is not None and row.last_seen_at == registered_seen_at

    for event in WorkerProgressEvent:
        recorder.note(event)
        recorder.note(event)
    assert sorted(await asyncio.gather(recorder.flush_now(), recorder.flush_now())) == [False, True]

    with worker_store() as session:
        row = session.get(WorkerProcess, recorder.generation_id)
        assert row is not None
        assert row.last_seen_at >= registered_seen_at
        assert {
            row.last_scan_at,
            row.last_claim_at,
            row.last_progress_at,
            row.last_lease_heartbeat_at,
        } == {row.last_seen_at}

    assert await recorder.stop() is True
    with worker_store() as session:
        row = session.get(WorkerProcess, recorder.generation_id)
        assert row is not None
        assert row.stopped_at is not None
        assert row.stopped_at >= row.last_seen_at

    recorder.note(WorkerProgressEvent.SCAN)
    assert await recorder.flush_now() is False
    with pytest.raises(WorkerProgressStateError):
        recorder._flush(_PendingEvent.SCAN)


@pytest.mark.asyncio
async def test_failed_flush_retains_pending_event_for_retry(worker_store, monkeypatch) -> None:
    recorder = WorkerProgressRecorder(
        worker_store,
        worker_id="worker-progress-retry",
        generation_id="00000000-0000-0000-0000-000000000002",
        flush_seconds=60,
    )
    await recorder.start()
    recorder.note(WorkerProgressEvent.CLAIM)
    original_flush = recorder._flush

    def fail_once(_pending):
        raise RuntimeError("controlled")

    monkeypatch.setattr(recorder, "_flush", fail_once)
    assert await recorder.flush_now() is False
    monkeypatch.setattr(recorder, "_flush", original_flush)
    assert await recorder.flush_now() is True
    await recorder.stop()

    with worker_store() as session:
        row = session.get(WorkerProcess, recorder.generation_id)
        assert row is not None and row.last_claim_at is not None


@pytest.mark.asyncio
async def test_stop_persists_preexisting_bits_and_drops_late_notes(
    worker_store,
    monkeypatch,
) -> None:
    recorder = WorkerProgressRecorder(
        worker_store,
        worker_id="worker-progress-stop-race",
        generation_id="00000000-0000-0000-0000-000000000003",
        flush_seconds=60,
    )
    await recorder.start()
    recorder.note(WorkerProgressEvent.SCAN)
    stop_entered = Event()
    release_stop = Event()
    original_stop = recorder._stop

    def controlled_stop(pending):
        stop_entered.set()
        assert release_stop.wait(timeout=2)
        original_stop(pending)

    monkeypatch.setattr(recorder, "_stop", controlled_stop)
    stopping = asyncio.create_task(recorder.stop())
    assert await asyncio.to_thread(stop_entered.wait, 2)
    recorder.note(WorkerProgressEvent.CLAIM)
    release_stop.set()
    assert await stopping is True

    with worker_store() as session:
        row = session.get(WorkerProcess, recorder.generation_id)
        assert row is not None
        assert row.last_scan_at == row.last_seen_at
        assert row.last_claim_at is None
        assert row.stopped_at == row.last_seen_at


def test_worker_progress_snapshot_uses_inclusive_live_boundary_and_ignores_stopped(
    worker_store,
) -> None:
    now = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)
    cutoff = now - timedelta(seconds=60)
    with worker_store() as session, session.begin():
        session.add_all(
            [
                WorkerProcess(
                    generation_id="00000000-0000-0000-0000-000000000011",
                    worker_id="worker-live-boundary",
                    started_at=cutoff - timedelta(seconds=1),
                    last_seen_at=cutoff,
                    last_scan_at=cutoff,
                ),
                WorkerProcess(
                    generation_id="00000000-0000-0000-0000-000000000012",
                    worker_id="worker-stalled",
                    started_at=cutoff - timedelta(seconds=10),
                    last_seen_at=cutoff - timedelta(microseconds=1),
                    last_progress_at=cutoff - timedelta(microseconds=1),
                ),
                WorkerProcess(
                    generation_id="00000000-0000-0000-0000-000000000013",
                    worker_id="worker-stopped",
                    started_at=cutoff,
                    last_seen_at=now,
                    last_claim_at=now,
                    stopped_at=now,
                ),
            ]
        )

    with worker_store() as session:
        snapshot = collect_worker_progress(
            session,
            now=now,
            stale_seconds=60,
            expected=3,
        )

    assert snapshot.registered == 2
    assert snapshot.live == 1
    assert snapshot.stalled == 1
    assert snapshot.shortfall == 2
    assert snapshot.last_seen_at == cutoff
    assert snapshot.last_scan_at == cutoff
    assert snapshot.last_progress_at == cutoff - timedelta(microseconds=1)
    assert snapshot.last_claim_at is None


def test_worker_timing_requires_stale_threshold_to_cover_every_cadence() -> None:
    with pytest.raises(ValidationError, match="worker_progress_stale_seconds"):
        Settings(
            _env_file=None,
            worker_heartbeat_seconds=10,
            worker_lease_seconds=30,
            worker_progress_flush_seconds=5,
            worker_progress_stale_seconds=29,
        )

    settings = Settings(
        _env_file=None,
        worker_heartbeat_seconds=10,
        worker_lease_seconds=30,
        worker_progress_stale_seconds=30,
        worker_expected_processes=0,
    )
    assert settings.worker_expected_processes == 0


def test_worker_process_constraints_reject_event_after_seen(worker_store) -> None:
    now = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)
    with pytest.raises(IntegrityError), worker_store() as session, session.begin():
        session.add(
            WorkerProcess(
                generation_id="00000000-0000-0000-0000-000000000021",
                worker_id="worker-invalid",
                started_at=now,
                last_seen_at=now,
                last_scan_at=now + timedelta(seconds=1),
            )
        )

    with worker_store() as session:
        assert session.scalar(select(WorkerProcess.generation_id)) is None
