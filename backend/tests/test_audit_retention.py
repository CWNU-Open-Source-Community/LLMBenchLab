"""SQLite audit retention archive/delete/reconcile/restore behavior."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.db.clock import database_utc_now
from app.db.session import SessionLocal
from app.governance.audit import append_audit_event, validate_audit_event_values_for_read
from app.governance.audit_archive import verify_archive, write_archive
from app.governance.audit_retention import (
    AuditRetentionCommitOutcomeUnknownError,
    AuditRetentionCommittedVerificationError,
    AuditRetentionError,
    archive_expired_events,
    delete_archive_events,
    reconcile_archive,
    restore_archive,
)
from app.models import AuditEvent, AuditRetentionClass


def _append_expired(session, *, index: int) -> None:
    now = database_utc_now(session)
    append_audit_event(
        session,
        event_key=f"retention:run:{index}:claimed",
        event_type="run_claimed",
        occurred_at=now - timedelta(days=91, minutes=index),
        payload={"dispatch_count": index},
        correlation_id=f"retention-run-{index}",
        run_id=f"retention-run-{index}",
        model_id=f"retention-model-{index}",
        worker_id=f"retention-worker-{index}",
        attempt=index,
        lease_token=index,
    )


def test_sqlite_archive_delete_reconcile_restore_round_trip(
    client,
    db_session,
    tmp_path,
) -> None:
    del client
    _append_expired(db_session, index=1)
    _append_expired(db_session, index=2)
    db_session.commit()

    output = tmp_path / "retention.jsonl"
    written = archive_expired_events(SessionLocal, output)
    archive = verify_archive(output, expected_sha256=written.archive_sha256)
    assert len(archive.events) == 2
    assert reconcile_archive(SessionLocal, archive).status == "all_exact"

    # An independently appended but already expired row is intentionally not
    # in this archive.  Exact delete must preserve it instead of deleting by
    # the archive cutoff.
    with SessionLocal() as session, session.begin():
        _append_expired(session, index=3)

    deleted = delete_archive_events(SessionLocal, archive)
    assert deleted.status == "deleted"
    assert deleted.changed_count == 2
    assert reconcile_archive(SessionLocal, archive).status == "all_absent"
    with SessionLocal() as session:
        assert (
            session.scalar(
                select(AuditEvent.event_key).where(
                    AuditEvent.event_key == "retention:run:3:claimed"
                )
            )
            == "retention:run:3:claimed"
        )
    assert delete_archive_events(SessionLocal, archive).status == "already_absent"

    restored = restore_archive(SessionLocal, archive)
    assert restored.status == "restored"
    assert restored.changed_count == 2
    assert reconcile_archive(SessionLocal, archive).status == "all_exact"
    assert restore_archive(SessionLocal, archive).status == "already_exact"


def test_delete_rejects_mixed_missing_and_conflicting_storage_facts(
    client,
    db_session,
    tmp_path,
) -> None:
    del client
    _append_expired(db_session, index=1)
    _append_expired(db_session, index=2)
    db_session.commit()
    output = tmp_path / "mixed.jsonl"
    archive_expired_events(SessionLocal, output)
    archive = verify_archive(output)

    with SessionLocal() as session, session.begin():
        session.execute(delete(AuditEvent).where(AuditEvent.id == archive.events[0].id))
    assert reconcile_archive(SessionLocal, archive).status == "mixed_exact_absent"
    with pytest.raises(AuditRetentionError, match="audit_retention_delete_conflict"):
        delete_archive_events(SessionLocal, archive)

    with SessionLocal() as session, session.begin():
        session.execute(
            update(AuditEvent)
            .where(AuditEvent.id == archive.events[1].id)
            .values(event_key="retention:conflicting:key")
        )
    assert reconcile_archive(SessionLocal, archive).status == "conflict"
    with pytest.raises(AuditRetentionError, match="audit_retention_restore_conflict"):
        restore_archive(SessionLocal, archive)


def test_delete_rolls_back_when_database_reports_a_partial_exact_delete(
    client,
    db_session,
    tmp_path,
) -> None:
    del client
    _append_expired(db_session, index=1)
    _append_expired(db_session, index=2)
    db_session.commit()
    output = tmp_path / "partial-delete.jsonl"
    archive_expired_events(SessionLocal, output)
    archive = verify_archive(output)

    with SessionLocal() as session, session.begin():
        session.execute(
            text(
                "CREATE TRIGGER ignore_one_audit_delete "
                "BEFORE DELETE ON audit_events "
                "WHEN OLD.event_key = 'retention:run:1:claimed' "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        )
    try:
        with pytest.raises(AuditRetentionError, match="audit_retention_delete_count_mismatch"):
            delete_archive_events(SessionLocal, archive)
        assert reconcile_archive(SessionLocal, archive).status == "all_exact"
    finally:
        with SessionLocal() as session, session.begin():
            session.execute(text("DROP TRIGGER IF EXISTS ignore_one_audit_delete"))


def test_restore_requires_referenced_reservation(client, tmp_path) -> None:
    del client
    occurred = datetime(2025, 1, 1, tzinfo=UTC)
    payload: dict[str, object] = {}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    event = validate_audit_event_values_for_read(
        id="00000000-0000-0000-0000-000000000099",
        event_key="retention:reservation:missing",
        event_type="provider_attempt_send_started",
        payload_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        payload=payload,
        retention_class=AuditRetentionClass.OPERATIONAL,
        occurred_at=occurred,
        expires_at=occurred + timedelta(days=90),
        correlation_id=None,
        run_id=None,
        model_id=None,
        question_id=None,
        worker_id=None,
        reservation_id="00000000-0000-0000-0000-000000000001",
        attempt=None,
        provider_attempt=1,
        lease_token=None,
        duration_ms=None,
    )
    output = tmp_path / "missing-reservation.jsonl"
    write_archive(
        output,
        (event,),
        cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_alembic_head="20260828_0005",
        has_more_eligible=False,
    )
    archive = verify_archive(output)
    with pytest.raises(AuditRetentionError, match="audit_retention_reservation_missing"):
        restore_archive(SessionLocal, archive)


def test_restore_reports_commit_outcome_unknown_and_reconcile_is_safe(
    client,
    db_session,
    tmp_path,
    monkeypatch,
) -> None:
    del client
    _append_expired(db_session, index=1)
    db_session.commit()
    output = tmp_path / "unknown.jsonl"
    archive_expired_events(SessionLocal, output)
    archive = verify_archive(output)
    delete_archive_events(SessionLocal, archive)

    original_commit = Session.commit

    def commit_then_raise(session: Session) -> None:
        original_commit(session)
        raise RuntimeError("simulated_ack_loss")

    monkeypatch.setattr(Session, "commit", commit_then_raise)
    with pytest.raises(AuditRetentionCommitOutcomeUnknownError):
        restore_archive(SessionLocal, archive)
    assert reconcile_archive(SessionLocal, archive).status == "all_exact"


def test_same_key_with_different_id_is_a_conflict(client, db_session, tmp_path) -> None:
    del client
    _append_expired(db_session, index=1)
    db_session.commit()
    output = tmp_path / "same-key.jsonl"
    archive_expired_events(SessionLocal, output)
    archive = verify_archive(output)
    with SessionLocal() as session, session.begin():
        session.execute(
            update(AuditEvent)
            .where(AuditEvent.id == archive.events[0].id)
            .values(id="00000000-0000-0000-0000-000000000777")
        )
    assert reconcile_archive(SessionLocal, archive).status == "conflict"
    with pytest.raises(AuditRetentionError, match="audit_retention_restore_conflict"):
        restore_archive(SessionLocal, archive)


def test_postcommit_verification_failure_is_distinct(
    client,
    db_session,
    tmp_path,
    monkeypatch,
) -> None:
    del client
    _append_expired(db_session, index=1)
    db_session.commit()
    output = tmp_path / "postcommit.jsonl"
    archive_expired_events(SessionLocal, output)
    archive = verify_archive(output)

    def fail_postcommit(_factory, _archive):
        raise AuditRetentionCommittedVerificationError("simulated_postcommit_failure")

    monkeypatch.setattr("app.governance.audit_retention._postcommit_report", fail_postcommit)
    with pytest.raises(AuditRetentionCommittedVerificationError):
        delete_archive_events(SessionLocal, archive)
    assert reconcile_archive(SessionLocal, archive).status == "all_absent"


def test_noop_restore_and_delete_still_run_independent_postverification(
    client,
    db_session,
    tmp_path,
    monkeypatch,
) -> None:
    del client
    _append_expired(db_session, index=1)
    db_session.commit()
    output = tmp_path / "noop-postverify.jsonl"
    archive_expired_events(SessionLocal, output)
    archive = verify_archive(output)

    real_postcommit = __import__(
        "app.governance.audit_retention", fromlist=["_postcommit_report"]
    )._postcommit_report
    observed_statuses: list[str] = []

    def record_postcommit(factory, verified_archive):
        report = real_postcommit(factory, verified_archive)
        observed_statuses.append(report.status)
        return report

    monkeypatch.setattr("app.governance.audit_retention._postcommit_report", record_postcommit)
    assert restore_archive(SessionLocal, archive).status == "already_exact"
    delete_archive_events(SessionLocal, archive)
    assert delete_archive_events(SessionLocal, archive).status == "already_absent"
    assert observed_statuses == ["all_exact", "all_absent", "all_absent"]


def test_commit_confirmed_cleanup_failure_is_exit_three_class(
    client,
    db_session,
    tmp_path,
    monkeypatch,
) -> None:
    del client
    _append_expired(db_session, index=1)
    db_session.commit()
    output = tmp_path / "cleanup.jsonl"
    archive_expired_events(SessionLocal, output)
    archive = verify_archive(output)
    original_close = Session.close

    def close_then_raise(session: Session) -> None:
        original_close(session)
        raise RuntimeError("simulated_close_failure")

    with monkeypatch.context() as patch:
        patch.setattr(Session, "close", close_then_raise)
        with pytest.raises(AuditRetentionCommittedVerificationError):
            delete_archive_events(SessionLocal, archive)
    assert reconcile_archive(SessionLocal, archive).status == "all_absent"


def test_archive_hard_cap_sets_has_more(client, db_session, tmp_path, monkeypatch) -> None:
    del client
    _append_expired(db_session, index=1)
    _append_expired(db_session, index=2)
    db_session.commit()
    monkeypatch.setattr("app.governance.audit_retention.ARCHIVE_EVENT_LIMIT", 1)
    output = tmp_path / "bounded.jsonl"
    result = archive_expired_events(SessionLocal, output)
    archive = verify_archive(output)
    assert result.event_count == 1
    assert result.has_more_eligible is True
    assert archive.has_more_eligible is True
    assert len(archive.events) == 1


def test_public_full_row_projection_rejects_corrupt_duration(client, db_session) -> None:
    del client
    _append_expired(db_session, index=1)
    db_session.commit()
    event = db_session.scalar(select(AuditEvent))
    assert event is not None
    event.duration_ms = float("inf")
    from app.governance.audit import AuditIntegrityError, validate_audit_event_for_read

    with pytest.raises(AuditIntegrityError):
        validate_audit_event_for_read(event)
