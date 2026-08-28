"""Database-authoritative audit archive, reconcile, restore, and exact delete operations."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.clock import database_utc_now
from app.db.prepare_migrations import database_heads, expected_database_heads
from app.governance.audit import (
    AuditEventReadFacts,
    AuditIntegrityError,
    validate_audit_event_for_read,
)
from app.governance.audit_archive import (
    ARCHIVE_EVENT_LIMIT,
    ARCHIVE_V1_COMPATIBLE_ALEMBIC_HEADS,
    VerifiedAuditArchive,
    WrittenAuditArchive,
    write_archive,
)
from app.governance.audit_retention_errors import (
    AuditRetentionCommitOutcomeUnknownError,
    AuditRetentionCommittedVerificationError,
    AuditRetentionError,
)
from app.models import AuditEvent, ProviderCallReservation

_POSTGRES_ADVISORY_LOCK = (1280068930, 1381256526)
_QUERY_CHUNK_SIZE = 400

ReconcileStatus = Literal[
    "all_exact",
    "all_absent",
    "mixed_exact_absent",
    "conflict",
    "empty_archive",
]


@dataclass(frozen=True)
class ReconcileReport:
    status: ReconcileStatus
    event_count: int
    exact_count: int
    absent_count: int
    conflict_count: int


@dataclass(frozen=True)
class MutationReport:
    status: str
    event_count: int
    changed_count: int
    archive_sha256: str


@dataclass
class _CommitState:
    confirmed: bool = False
    event_count: int = 0
    archive_sha256: str = ""


@contextmanager
def _mutation_session(
    session_factory: sessionmaker[Session],
    state: _CommitState,
) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        try:
            session.close()
        except Exception as exc:
            if state.confirmed:
                raise AuditRetentionCommittedVerificationError(
                    "audit_retention_committed_cleanup_failed",
                    event_count=state.event_count,
                    archive_sha256=state.archive_sha256,
                ) from exc


def _configure_read_snapshot(session: Session) -> None:
    connection = session.connection()
    dialect = connection.dialect.name
    if dialect == "sqlite":
        connection.exec_driver_sql("BEGIN")
    elif dialect == "postgresql":
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    else:
        raise AuditRetentionError("audit_retention_database_dialect_unsupported")


def _require_current_head(session: Session) -> str:
    current = tuple(sorted(database_heads(session.connection())))
    expected = tuple(sorted(expected_database_heads()))
    if current != expected or len(current) != 1:
        raise AuditRetentionError("audit_retention_database_schema_not_ready")
    if current[0] not in ARCHIVE_V1_COMPATIBLE_ALEMBIC_HEADS:
        raise AuditRetentionError("audit_retention_database_schema_not_compatible")
    return current[0]


def archive_expired_events(
    session_factory: sessionmaker[Session],
    output: Path,
) -> WrittenAuditArchive:
    """Snapshot currently expired events and atomically write one bounded archive."""

    with session_factory() as session:
        try:
            _configure_read_snapshot(session)
            source_head = _require_current_head(session)
            cutoff_at = database_utc_now(session)
            rows = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.expires_at < cutoff_at)
                .order_by(AuditEvent.expires_at, AuditEvent.id)
                .limit(ARCHIVE_EVENT_LIMIT + 1)
            ).all()
            selected = rows[:ARCHIVE_EVENT_LIMIT]
            events = tuple(validate_audit_event_for_read(event) for event in selected)
            has_more = len(rows) > ARCHIVE_EVENT_LIMIT
        except AuditRetentionError:
            raise
        except AuditIntegrityError:
            raise AuditRetentionError("audit_retention_event_integrity_error") from None
        except Exception as exc:
            raise AuditRetentionError("audit_retention_archive_snapshot_failed") from exc
        finally:
            session.rollback()

    return write_archive(
        output,
        events,
        cutoff_at=cutoff_at,
        source_alembic_head=source_head,
        has_more_eligible=has_more,
    )


def _configure_mutation_transaction(session: Session) -> None:
    connection = session.connection()
    dialect = connection.dialect.name
    if dialect == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif dialect == "postgresql":
        connection.exec_driver_sql(
            f"SELECT pg_advisory_xact_lock({_POSTGRES_ADVISORY_LOCK[0]}, "
            f"{_POSTGRES_ADVISORY_LOCK[1]})"
        )
    else:
        raise AuditRetentionError("audit_retention_database_dialect_unsupported")


def _chunks(values: tuple[AuditEventReadFacts, ...]):
    for start in range(0, len(values), _QUERY_CHUNK_SIZE):
        yield values[start : start + _QUERY_CHUNK_SIZE]


def _load_candidate_rows(
    session: Session,
    events: tuple[AuditEventReadFacts, ...],
    *,
    for_update: bool,
) -> list[AuditEvent]:
    rows_by_identity: dict[str, AuditEvent] = {}
    for chunk in _chunks(events):
        ids = [event.id for event in chunk]
        keys = [event.event_key for event in chunk]
        statement = select(AuditEvent).where(
            or_(AuditEvent.id.in_(ids), AuditEvent.event_key.in_(keys))
        )
        if for_update and session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        for row in session.scalars(statement):
            rows_by_identity[row.id] = row
    return list(rows_by_identity.values())


def _classify(
    events: tuple[AuditEventReadFacts, ...],
    rows: list[AuditEvent],
) -> tuple[ReconcileReport, dict[str, AuditEvent]]:
    by_id = {row.id: row for row in rows}
    by_key = {row.event_key: row for row in rows}
    exact_count = 0
    absent_count = 0
    conflict_count = 0
    exact_rows: dict[str, AuditEvent] = {}
    for expected in events:
        id_row = by_id.get(expected.id)
        key_row = by_key.get(expected.event_key)
        if id_row is None and key_row is None:
            absent_count += 1
            continue
        if id_row is None or key_row is None or id_row.id != key_row.id:
            conflict_count += 1
            continue
        try:
            actual = validate_audit_event_for_read(id_row)
        except AuditIntegrityError:
            conflict_count += 1
            continue
        if actual == expected:
            exact_count += 1
            exact_rows[expected.id] = id_row
        else:
            conflict_count += 1

    event_count = len(events)
    if event_count == 0:
        status: ReconcileStatus = "empty_archive"
    elif conflict_count:
        status = "conflict"
    elif exact_count == event_count:
        status = "all_exact"
    elif absent_count == event_count:
        status = "all_absent"
    else:
        status = "mixed_exact_absent"
    return (
        ReconcileReport(
            status=status,
            event_count=event_count,
            exact_count=exact_count,
            absent_count=absent_count,
            conflict_count=conflict_count,
        ),
        exact_rows,
    )


def _reconcile_in_session(
    session: Session,
    archive: VerifiedAuditArchive,
    *,
    for_update: bool,
) -> tuple[ReconcileReport, dict[str, AuditEvent]]:
    rows = _load_candidate_rows(session, archive.events, for_update=for_update)
    return _classify(archive.events, rows)


def reconcile_archive(
    session_factory: sessionmaker[Session],
    archive: VerifiedAuditArchive,
) -> ReconcileReport:
    """Read-only exact reconciliation for commit-unknown recovery."""

    with session_factory() as session:
        try:
            _configure_read_snapshot(session)
            _require_current_head(session)
            report, _rows = _reconcile_in_session(session, archive, for_update=False)
            return report
        except AuditRetentionError:
            raise
        except Exception as exc:
            raise AuditRetentionError("audit_retention_reconcile_failed") from exc
        finally:
            session.rollback()


def _require_reservations(
    session: Session,
    events: tuple[AuditEventReadFacts, ...],
) -> None:
    reservation_ids = tuple(
        sorted({event.reservation_id for event in events if event.reservation_id is not None})
    )
    if not reservation_ids:
        return
    found: set[str] = set()
    for start in range(0, len(reservation_ids), _QUERY_CHUNK_SIZE):
        chunk = reservation_ids[start : start + _QUERY_CHUNK_SIZE]
        found.update(
            session.scalars(
                select(ProviderCallReservation.id).where(ProviderCallReservation.id.in_(chunk))
            )
        )
    if found != set(reservation_ids):
        raise AuditRetentionError("audit_retention_reservation_missing")


def _postcommit_report(
    session_factory: sessionmaker[Session],
    archive: VerifiedAuditArchive,
) -> ReconcileReport:
    try:
        return reconcile_archive(session_factory, archive)
    except Exception as exc:
        raise AuditRetentionCommittedVerificationError(
            "audit_retention_postcommit_verification_failed",
            event_count=len(archive.events),
            archive_sha256=archive.archive_sha256,
        ) from exc


def _delete_exact_event_ids(
    session: Session,
    events: tuple[AuditEventReadFacts, ...],
) -> int:
    """Delete the already-locked exact ID set and return the driver rowcount."""

    deleted_count = 0
    for chunk in _chunks(events):
        result = session.execute(
            delete(AuditEvent)
            .where(AuditEvent.id.in_([event.id for event in chunk]))
            .execution_options(synchronize_session=False)
        )
        rowcount = getattr(result, "rowcount", None)
        if isinstance(rowcount, bool) or not isinstance(rowcount, int) or rowcount < 0:
            raise RuntimeError("audit_retention_delete_rowcount_unavailable")
        deleted_count += rowcount
    return deleted_count


def restore_archive(
    session_factory: sessionmaker[Session],
    archive: VerifiedAuditArchive,
) -> MutationReport:
    """Insert only absent exact rows and reject every conflicting durable fact."""

    changed_count = 0
    commit_state = _CommitState(
        event_count=len(archive.events),
        archive_sha256=archive.archive_sha256,
    )
    with _mutation_session(session_factory, commit_state) as session:
        try:
            _configure_mutation_transaction(session)
            _require_current_head(session)
            _require_reservations(session, archive.events)
            report, _exact_rows = _reconcile_in_session(session, archive, for_update=True)
            if report.status == "conflict":
                raise AuditRetentionError("audit_retention_restore_conflict")
            if report.status in {"all_absent", "mixed_exact_absent"}:
                rows = _load_candidate_rows(session, archive.events, for_update=False)
                existing_ids = {row.id for row in rows}
                existing_keys = {row.event_key for row in rows}
                missing = [
                    event
                    for event in archive.events
                    if event.id not in existing_ids and event.event_key not in existing_keys
                ]
                for event in missing:
                    session.add(AuditEvent(**event.as_insert_values()))
                changed_count = len(missing)
                try:
                    session.flush()
                except Exception as exc:
                    session.rollback()
                    raise AuditRetentionError("audit_retention_restore_precommit_failed") from exc
                try:
                    session.commit()
                except Exception as exc:
                    raise AuditRetentionCommitOutcomeUnknownError(
                        "audit_retention_restore_commit_outcome_unknown",
                        event_count=len(archive.events),
                        archive_sha256=archive.archive_sha256,
                    ) from exc
                commit_state.confirmed = True
            else:
                session.rollback()
        except (AuditRetentionError, AuditRetentionCommitOutcomeUnknownError):
            raise
        except Exception as exc:
            if commit_state.confirmed:
                raise AuditRetentionCommittedVerificationError(
                    "audit_retention_restore_committed_cleanup_failed",
                    event_count=len(archive.events),
                    archive_sha256=archive.archive_sha256,
                ) from exc
            session.rollback()
            raise AuditRetentionError("audit_retention_restore_precommit_failed") from exc

    postcommit = _postcommit_report(session_factory, archive)
    if postcommit.status not in {"all_exact", "empty_archive"}:
        raise AuditRetentionCommittedVerificationError(
            "audit_retention_restore_postcommit_mismatch",
            event_count=len(archive.events),
            archive_sha256=archive.archive_sha256,
        )
    return MutationReport(
        status="restored" if changed_count else "already_exact",
        event_count=len(archive.events),
        changed_count=changed_count,
        archive_sha256=archive.archive_sha256,
    )


def delete_archive_events(
    session_factory: sessionmaker[Session],
    archive: VerifiedAuditArchive,
) -> MutationReport:
    """Delete only the exact archived id/key set after expiry and full comparison."""

    changed_count = 0
    commit_state = _CommitState(
        event_count=len(archive.events),
        archive_sha256=archive.archive_sha256,
    )
    with _mutation_session(session_factory, commit_state) as session:
        try:
            _configure_mutation_transaction(session)
            _require_current_head(session)
            database_now = database_utc_now(session)
            if database_now < archive.cutoff_at or any(
                event.expires_at >= database_now for event in archive.events
            ):
                raise AuditRetentionError("audit_retention_delete_not_expired")
            report, _exact_rows = _reconcile_in_session(session, archive, for_update=True)
            if report.status in {"conflict", "mixed_exact_absent"}:
                raise AuditRetentionError("audit_retention_delete_conflict")
            if report.status == "all_exact":
                try:
                    actual_deleted_count = _delete_exact_event_ids(session, archive.events)
                except Exception as exc:
                    session.rollback()
                    raise AuditRetentionError("audit_retention_delete_precommit_failed") from exc
                if actual_deleted_count != len(archive.events):
                    session.rollback()
                    raise AuditRetentionError("audit_retention_delete_count_mismatch")
                changed_count = actual_deleted_count
                try:
                    session.commit()
                except Exception as exc:
                    raise AuditRetentionCommitOutcomeUnknownError(
                        "audit_retention_delete_commit_outcome_unknown",
                        event_count=len(archive.events),
                        archive_sha256=archive.archive_sha256,
                    ) from exc
                commit_state.confirmed = True
            else:
                session.rollback()
        except (AuditRetentionError, AuditRetentionCommitOutcomeUnknownError):
            raise
        except Exception as exc:
            if commit_state.confirmed:
                raise AuditRetentionCommittedVerificationError(
                    "audit_retention_delete_committed_cleanup_failed",
                    event_count=len(archive.events),
                    archive_sha256=archive.archive_sha256,
                ) from exc
            session.rollback()
            raise AuditRetentionError("audit_retention_delete_precommit_failed") from exc

    postcommit = _postcommit_report(session_factory, archive)
    if postcommit.status not in {"all_absent", "empty_archive"}:
        raise AuditRetentionCommittedVerificationError(
            "audit_retention_delete_postcommit_mismatch",
            event_count=len(archive.events),
            archive_sha256=archive.archive_sha256,
        )
    return MutationReport(
        status="deleted" if changed_count else "already_absent",
        event_count=len(archive.events),
        changed_count=changed_count,
        archive_sha256=archive.archive_sha256,
    )
