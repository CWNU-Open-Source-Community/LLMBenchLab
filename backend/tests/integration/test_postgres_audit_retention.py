"""Real PostgreSQL audit-retention snapshot, advisory lock, and round trip."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from time import monotonic, sleep

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.db.clock import database_utc_now
from app.db.session import create_database_engine
from app.governance.audit import append_audit_event
from app.governance.audit_archive import verify_archive
from app.governance.audit_retention import (
    _POSTGRES_ADVISORY_LOCK,
    _configure_mutation_transaction,
    _reconcile_in_session,
    archive_expired_events,
    delete_archive_events,
    reconcile_archive,
    restore_archive,
)
from app.models import AuditEvent

pytestmark = pytest.mark.integration


def _wait_for_ungranted_lock(
    session_factory: sessionmaker[Session],
    *,
    backend_pid: int,
    lock_kind: str,
    timeout_seconds: float = 5.0,
) -> bool:
    if lock_kind == "advisory":
        statement = text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_locks WHERE pid = :backend_pid AND granted IS FALSE "
            "AND locktype = 'advisory' AND classid = :classid AND objid = :objid "
            "AND objsubid = 2)"
        )
        parameters = {
            "backend_pid": backend_pid,
            "classid": _POSTGRES_ADVISORY_LOCK[0],
            "objid": _POSTGRES_ADVISORY_LOCK[1],
        }
    elif lock_kind == "row":
        statement = text(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_locks WHERE pid = :backend_pid AND granted IS FALSE "
            "AND locktype IN ('transactionid', 'tuple'))"
        )
        parameters = {"backend_pid": backend_pid}
    else:
        raise ValueError(f"unsupported lock kind: {lock_kind}")

    deadline = monotonic() + timeout_seconds
    with session_factory() as monitor:
        while monotonic() < deadline:
            if bool(monitor.scalar(statement, parameters)):
                return True
            sleep(0.01)
    return False


@pytest.fixture
def postgres_retention_store():
    database_url = os.environ.get("LLMBENCHLAB_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("LLMBENCHLAB_TEST_POSTGRES_URL is required")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("LLMBENCHLAB_TEST_POSTGRES_URL must use PostgreSQL")
    database_name = url.database or ""
    if (
        "test" not in database_name.lower()
        and os.environ.get("LLMBENCHLAB_TEST_ALLOW_TRUNCATE") != "1"
    ):
        pytest.fail(
            "PostgreSQL integration tests require a database name containing 'test' or "
            "LLMBENCHLAB_TEST_ALLOW_TRUNCATE=1"
        )
    engine = create_database_engine(database_url)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
    try:
        yield factory
    finally:
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE audit_events RESTART IDENTITY CASCADE"))
        engine.dispose()


def test_postgres_mutation_advisory_lock_serializes_second_connection(
    postgres_retention_store,
) -> None:
    first = postgres_retention_store()
    contender_pid: Queue[int] = Queue(maxsize=1)

    def contend_for_mutation_lock() -> int:
        with postgres_retention_store() as session:
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            backend_pid = int(session.scalar(text("SELECT pg_backend_pid()")))
            contender_pid.put(backend_pid)
            _configure_mutation_transaction(session)
            session.rollback()
            return backend_pid

    try:
        _configure_mutation_transaction(first)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(contend_for_mutation_lock)
            backend_pid = contender_pid.get(timeout=5)
            try:
                assert _wait_for_ungranted_lock(
                    postgres_retention_store,
                    backend_pid=backend_pid,
                    lock_kind="advisory",
                )
                assert not future.done()
            finally:
                first.rollback()
            assert future.result(timeout=5) == backend_pid
    finally:
        first.rollback()
        first.close()


def test_postgres_exact_reconcile_for_update_locks_audit_row(
    postgres_retention_store,
    tmp_path,
) -> None:
    with postgres_retention_store() as session, session.begin():
        now = database_utc_now(session)
        append_audit_event(
            session,
            event_key="pg-retention:row-lock",
            event_type="run_claimed",
            occurred_at=now - timedelta(days=91),
            payload={"dispatch_count": 1},
            worker_id="pg-retention-worker",
            attempt=1,
            lease_token=1,
        )

    output = tmp_path / "postgres-retention-row-lock.jsonl"
    written = archive_expired_events(postgres_retention_store, output)
    archive = verify_archive(output, expected_sha256=written.archive_sha256)
    assert len(archive.events) == 1
    event_id = archive.events[0].id

    first = postgres_retention_store()
    contender_pid: Queue[int] = Queue(maxsize=1)

    def update_locked_row() -> tuple[int, int]:
        with postgres_retention_store() as session:
            session.execute(text("SET LOCAL statement_timeout = '10s'"))
            backend_pid = int(session.scalar(text("SELECT pg_backend_pid()")))
            contender_pid.put(backend_pid)
            result = session.execute(
                text("UPDATE audit_events SET event_type = event_type WHERE id = :event_id"),
                {"event_id": event_id},
            )
            rowcount = int(result.rowcount)
            session.rollback()
            return backend_pid, rowcount

    try:
        report, exact_rows = _reconcile_in_session(first, archive, for_update=True)
        assert report.status == "all_exact"
        assert set(exact_rows) == {event_id}
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(update_locked_row)
            backend_pid = contender_pid.get(timeout=5)
            try:
                assert _wait_for_ungranted_lock(
                    postgres_retention_store,
                    backend_pid=backend_pid,
                    lock_kind="row",
                )
                assert not future.done()
            finally:
                first.rollback()
            assert future.result(timeout=5) == (backend_pid, 1)
    finally:
        first.rollback()
        first.close()


def test_postgres_archive_delete_reconcile_restore_round_trip(
    postgres_retention_store,
    tmp_path,
) -> None:
    with postgres_retention_store() as session, session.begin():
        now = database_utc_now(session)
        append_audit_event(
            session,
            event_key="pg-retention:expired",
            event_type="run_claimed",
            occurred_at=now - timedelta(days=91),
            payload={"dispatch_count": 1},
            worker_id="pg-retention-worker",
            attempt=1,
            lease_token=1,
        )
        append_audit_event(
            session,
            event_key="pg-retention:future",
            event_type="run_claimed",
            occurred_at=now,
            payload={"dispatch_count": 2},
            worker_id="pg-retention-worker",
            attempt=1,
            lease_token=1,
        )

    output = tmp_path / "postgres-retention.jsonl"
    written = archive_expired_events(postgres_retention_store, output)
    archive = verify_archive(output, expected_sha256=written.archive_sha256)
    assert len(archive.events) == 1
    assert archive.events[0].event_key == "pg-retention:expired"
    assert reconcile_archive(postgres_retention_store, archive).status == "all_exact"

    deleted = delete_archive_events(postgres_retention_store, archive)
    assert deleted.status == "deleted"
    assert deleted.changed_count == 1
    assert reconcile_archive(postgres_retention_store, archive).status == "all_absent"
    with postgres_retention_store() as session:
        assert session.scalar(select(func.count(AuditEvent.id))) == 1

    restored = restore_archive(postgres_retention_store, archive)
    assert restored.status == "restored"
    assert reconcile_archive(postgres_retention_store, archive).status == "all_exact"
    with postgres_retention_store() as session:
        assert session.scalar(select(func.count(AuditEvent.id))) == 2
