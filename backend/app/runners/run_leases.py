"""Database-backed Run leases, fencing, retries, cancellation, and response idempotency."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import EvaluationResponse, EvaluationRun, RunStatus

DatabaseClock = Callable[[Session], datetime]


class AttemptDisposition(StrEnum):
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    RECOVERED_COMPLETED = "recovered_completed"
    CANCELLED = "cancelled"
    FENCE_LOST = "fence_lost"


class CancelDisposition(StrEnum):
    CANCELLED = "cancelled"
    REQUESTED = "requested"
    TERMINAL = "terminal"
    NOT_FOUND = "not_found"


class ResponseDisposition(StrEnum):
    INSERTED = "inserted"
    ALREADY_PRESENT = "already_present"
    CANCEL_REQUESTED = "cancel_requested"
    FENCE_LOST = "fence_lost"


@dataclass(frozen=True, slots=True)
class RunLease:
    run_id: str
    owner: str
    token: int
    attempt: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReapReport:
    cancelled: int = 0
    dead_lettered: int = 0
    completed: int = 0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _database_clock(session: Session) -> datetime:
    value = session.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("Database did not return a timestamp for lease coordination")
    return _as_utc(value)


def _safe_error_code(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    if not normalized or not re.fullmatch(r"[a-z0-9_:.]+", normalized):
        return "worker_internal_error"
    return normalized[:128]


def _acquire_sqlite_transition_lock(session: Session) -> None:
    """Serialize SQLite state transitions before their first read.

    PostgreSQL uses row locks and conditional updates. SQLite ignores ``FOR UPDATE``,
    so its supported single-Worker path takes the database write lock up front to
    prevent an API cancellation from racing a stale lease transition.
    """

    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def aggregate_run_evidence(session: Session, run: EvaluationRun) -> int:
    """Recompute protocol-v1 aggregates from persisted Response facts."""

    aggregate = session.execute(
        select(
            func.count(EvaluationResponse.id),
            func.coalesce(func.sum(EvaluationResponse.score), 0.0),
            func.count(EvaluationResponse.id).filter(
                EvaluationResponse.raw_response.is_not(None),
                EvaluationResponse.raw_response != "",
            ),
            func.count(EvaluationResponse.id).filter(
                EvaluationResponse.error_type.is_(None),
                EvaluationResponse.raw_response.is_not(None),
                EvaluationResponse.raw_response != "",
            ),
            func.count(EvaluationResponse.id).filter(EvaluationResponse.error_type.is_not(None)),
            func.avg(EvaluationResponse.latency_ms),
            func.coalesce(func.sum(EvaluationResponse.input_tokens), 0),
            func.count(EvaluationResponse.input_tokens),
            func.coalesce(func.sum(EvaluationResponse.output_tokens), 0),
            func.count(EvaluationResponse.output_tokens),
            func.coalesce(func.sum(EvaluationResponse.estimated_cost), 0),
            func.count(EvaluationResponse.estimated_cost),
        ).where(EvaluationResponse.run_id == run.id)
    ).one()
    (
        response_count,
        score_sum,
        completed_outputs,
        evaluable,
        errors,
        avg_latency,
        in_tok,
        in_reports,
        out_tok,
        out_reports,
        cost,
        cost_reports,
    ) = aggregate
    planned = run.total_questions
    completed_response_count = int(response_count or 0)
    correct = round(float(score_sum or 0))
    run.completed_questions = completed_response_count
    run.correct_questions = correct
    run.error_questions = int(errors or 0)
    run.score = (float(score_sum or 0) / planned * 100) if planned else 0.0
    run.completion_rate = (int(completed_outputs or 0) / planned * 100) if planned else 0.0
    run.answered_accuracy = (correct / int(evaluable) * 100) if int(evaluable or 0) else None
    run.average_latency_ms = float(avg_latency) if avg_latency is not None else None
    run.input_tokens = (
        int(in_tok or 0)
        if completed_response_count and int(in_reports or 0) == completed_response_count
        else None
    )
    run.output_tokens = (
        int(out_tok or 0)
        if completed_response_count and int(out_reports or 0) == completed_response_count
        else None
    )
    run.estimated_cost = (
        Decimal(cost or 0)
        if completed_response_count and int(cost_reports or 0) == completed_response_count
        else None
    )
    return completed_response_count


class RunLeaseRepository:
    """Coordinate Run ownership exclusively through short database transactions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_for: timedelta = timedelta(seconds=30),
        retry_backoff_base: timedelta = timedelta(seconds=1),
        retry_backoff_cap: timedelta = timedelta(seconds=30),
        clock: DatabaseClock = _database_clock,
    ) -> None:
        if lease_for.total_seconds() <= 0:
            raise ValueError("lease_for must be positive")
        if retry_backoff_base.total_seconds() < 0:
            raise ValueError("retry_backoff_base must not be negative")
        if retry_backoff_cap < retry_backoff_base:
            raise ValueError("retry_backoff_cap must not be shorter than retry_backoff_base")
        self._session_factory = session_factory
        self._lease_for = lease_for
        self._retry_backoff_base = retry_backoff_base
        self._retry_backoff_cap = retry_backoff_cap
        self._clock = clock

    @staticmethod
    def _eligible(now: datetime):
        response_count = (
            select(func.count(EvaluationResponse.id))
            .where(EvaluationResponse.run_id == EvaluationRun.id)
            .correlate(EvaluationRun)
            .scalar_subquery()
        )
        return (
            (EvaluationRun.cancellation_requested.is_(False))
            & (EvaluationRun.attempt_count < EvaluationRun.max_attempts)
            & (response_count < EvaluationRun.total_questions)
            & or_(
                (EvaluationRun.status == RunStatus.PENDING)
                & or_(
                    EvaluationRun.next_attempt_at.is_(None),
                    EvaluationRun.next_attempt_at <= now,
                ),
                (EvaluationRun.status == RunStatus.RUNNING)
                & EvaluationRun.lease_expires_at.is_not(None)
                & (EvaluationRun.lease_expires_at <= now),
            )
        )

    def claim(self, run_id: str, *, owner: str) -> RunLease | None:
        if not owner or len(owner) > 128:
            raise ValueError("owner must contain 1 to 128 characters")
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            expires_at = now + self._lease_for
            result = session.execute(
                update(EvaluationRun)
                .where(EvaluationRun.id == run_id, self._eligible(now))
                .values(
                    status=RunStatus.RUNNING,
                    attempt_count=EvaluationRun.attempt_count + 1,
                    lease_owner=owner,
                    lease_token=EvaluationRun.lease_token + 1,
                    lease_expires_at=expires_at,
                    heartbeat_at=now,
                    next_attempt_at=None,
                    finished_at=None,
                    started_at=func.coalesce(EvaluationRun.started_at, now),
                    last_error=case(
                        (
                            EvaluationRun.status == RunStatus.RUNNING,
                            "worker_lease_expired",
                        ),
                        else_=EvaluationRun.last_error,
                    ),
                )
            )
            if result.rowcount != 1:
                return None
            row = session.execute(
                select(
                    EvaluationRun.lease_token,
                    EvaluationRun.attempt_count,
                    EvaluationRun.lease_expires_at,
                ).where(EvaluationRun.id == run_id, EvaluationRun.lease_owner == owner)
            ).one()
            return RunLease(
                run_id=run_id,
                owner=owner,
                token=int(row.lease_token),
                attempt=int(row.attempt_count),
                expires_at=_as_utc(row.lease_expires_at),
            )

    def due_run_ids(self, *, limit: int = 100) -> tuple[str, ...]:
        if limit < 1:
            return ()
        with self._session_factory() as session:
            now = self._clock(session)
            expired_first = case((EvaluationRun.status == RunStatus.RUNNING, 0), else_=1)
            return tuple(
                session.scalars(
                    select(EvaluationRun.id)
                    .where(self._eligible(now))
                    .order_by(expired_first, EvaluationRun.created_at, EvaluationRun.id)
                    .limit(limit)
                )
            )

    def claim_next(self, *, owner: str, scan_limit: int = 100) -> RunLease | None:
        for run_id in self.due_run_ids(limit=scan_limit):
            lease = self.claim(run_id, owner=owner)
            if lease is not None:
                return lease
        return None

    def heartbeat(self, lease: RunLease) -> RunLease | None:
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            expires_at = now + self._lease_for
            result = session.execute(
                update(EvaluationRun)
                .where(
                    EvaluationRun.id == lease.run_id,
                    EvaluationRun.status == RunStatus.RUNNING,
                    EvaluationRun.cancellation_requested.is_(False),
                    EvaluationRun.lease_owner == lease.owner,
                    EvaluationRun.lease_token == lease.token,
                    EvaluationRun.lease_expires_at > now,
                )
                .values(heartbeat_at=now, lease_expires_at=expires_at)
            )
            if result.rowcount != 1:
                return None
            return RunLease(
                run_id=lease.run_id,
                owner=lease.owner,
                token=lease.token,
                attempt=lease.attempt,
                expires_at=expires_at,
            )

    def _locked_run(
        self,
        session: Session,
        lease: RunLease,
        now: datetime,
    ) -> EvaluationRun | None:
        return session.scalar(
            select(EvaluationRun)
            .where(
                EvaluationRun.id == lease.run_id,
                EvaluationRun.status == RunStatus.RUNNING,
                EvaluationRun.lease_owner == lease.owner,
                EvaluationRun.lease_token == lease.token,
                EvaluationRun.lease_expires_at > now,
            )
            .with_for_update()
        )

    def lock_owned_run(
        self,
        session: Session,
        lease: RunLease,
        *,
        allow_cancel_requested: bool = False,
    ) -> EvaluationRun | None:
        """Lock and return the currently fenced Run inside the caller's transaction."""

        _acquire_sqlite_transition_lock(session)
        run = self._locked_run(session, lease, self._clock(session))
        if run is None or (run.cancellation_requested and not allow_cancel_requested):
            return None
        return run

    def persist_response(
        self,
        lease: RunLease,
        response: EvaluationResponse,
    ) -> ResponseDisposition:
        try:
            with self._session_factory() as session, session.begin():
                _acquire_sqlite_transition_lock(session)
                now = self._clock(session)
                run = self._locked_run(session, lease, now)
                if run is None:
                    return ResponseDisposition.FENCE_LOST
                if run.cancellation_requested:
                    return ResponseDisposition.CANCEL_REQUESTED
                existing = session.scalar(
                    select(EvaluationResponse.id).where(
                        EvaluationResponse.run_id == lease.run_id,
                        EvaluationResponse.question_id == response.question_id,
                    )
                )
                if existing is not None:
                    return ResponseDisposition.ALREADY_PRESENT
                session.add(response)
                session.flush()
                run.completed_questions = int(
                    session.scalar(
                        select(func.count(EvaluationResponse.id)).where(
                            EvaluationResponse.run_id == lease.run_id
                        )
                    )
                    or 0
                )
                return ResponseDisposition.INSERTED
        except IntegrityError:
            with self._session_factory() as session:
                existing = session.scalar(
                    select(EvaluationResponse.id).where(
                        EvaluationResponse.run_id == lease.run_id,
                        EvaluationResponse.question_id == response.question_id,
                    )
                )
            if existing is not None:
                return ResponseDisposition.ALREADY_PRESENT
            raise

    def request_cancel(self, run_id: str) -> CancelDisposition:
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            active_status = session.scalar(
                update(EvaluationRun)
                .where(
                    EvaluationRun.id == run_id,
                    EvaluationRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
                )
                .values(cancellation_requested=True)
                .returning(EvaluationRun.status)
            )
            if active_status == RunStatus.PENDING:
                run = session.get(EvaluationRun, run_id)
                if run is None:  # The row cannot disappear inside this transaction.
                    raise RuntimeError("cancelled_run_missing")
                aggregate_run_evidence(session, run)
                run.status = RunStatus.CANCELLED
                run.finished_at = now
                run.next_attempt_at = None
                return CancelDisposition.CANCELLED
            if active_status == RunStatus.RUNNING:
                return CancelDisposition.REQUESTED
            status_value = session.scalar(
                select(EvaluationRun.status).where(EvaluationRun.id == run_id)
            )
            return (
                CancelDisposition.NOT_FOUND if status_value is None else CancelDisposition.TERMINAL
            )

    def finish_cancelled(self, lease: RunLease) -> bool:
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            run = self._locked_run(session, lease, now)
            if run is None or not run.cancellation_requested:
                return False
            aggregate_run_evidence(session, run)
            run.status = RunStatus.CANCELLED
            run.finished_at = now
            self._clear_lease(run)
            return True

    def fail_attempt(self, lease: RunLease, *, error_code: str) -> AttemptDisposition:
        safe_error = _safe_error_code(error_code)
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            run = self._locked_run(session, lease, now)
            if run is None:
                return AttemptDisposition.FENCE_LOST
            if run.cancellation_requested:
                aggregate_run_evidence(session, run)
                run.status = RunStatus.CANCELLED
                run.finished_at = now
                self._clear_lease(run)
                return AttemptDisposition.CANCELLED
            run.last_error = safe_error
            if self._complete_from_evidence(session, run, now):
                return AttemptDisposition.RECOVERED_COMPLETED
            if run.attempt_count >= run.max_attempts:
                run.status = RunStatus.FAILED
                run.dead_lettered_at = now
                run.finished_at = now
                run.error_message = safe_error
                self._clear_lease(run)
                return AttemptDisposition.DEAD_LETTERED
            exponent = max(run.attempt_count - 1, 0)
            delay_seconds = min(
                self._retry_backoff_base.total_seconds() * (2**exponent),
                self._retry_backoff_cap.total_seconds(),
            )
            run.status = RunStatus.PENDING
            run.next_attempt_at = now + timedelta(seconds=delay_seconds)
            self._clear_lease(run)
            return AttemptDisposition.RETRY_SCHEDULED

    def reap_expired(self, *, limit: int = 100) -> ReapReport:
        if limit < 1:
            return ReapReport()
        cancelled = 0
        dead_lettered = 0
        completed = 0
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            run_ids: Sequence[str] = tuple(
                session.scalars(
                    select(EvaluationRun.id)
                    .where(
                        EvaluationRun.status == RunStatus.RUNNING,
                        EvaluationRun.lease_expires_at <= now,
                    )
                    .order_by(EvaluationRun.lease_expires_at, EvaluationRun.id)
                    .limit(limit)
                )
            )
            for run_id in run_ids:
                run = session.scalar(
                    select(EvaluationRun).where(EvaluationRun.id == run_id).with_for_update()
                )
                if (
                    run is None
                    or run.status != RunStatus.RUNNING
                    or run.lease_expires_at is None
                    or _as_utc(run.lease_expires_at) > now
                ):
                    continue
                if run.cancellation_requested:
                    aggregate_run_evidence(session, run)
                    run.status = RunStatus.CANCELLED
                    run.finished_at = now
                    run.next_attempt_at = None
                    self._clear_lease(run)
                    cancelled += 1
                elif self._complete_from_evidence(session, run, now):
                    completed += 1
                elif run.attempt_count >= run.max_attempts:
                    error_code = "worker_lease_expired_retry_exhausted"
                    run.last_error = error_code
                    run.status = RunStatus.FAILED
                    run.error_message = error_code
                    run.dead_lettered_at = now
                    run.finished_at = now
                    self._clear_lease(run)
                    dead_lettered += 1
        return ReapReport(
            cancelled=cancelled,
            dead_lettered=dead_lettered,
            completed=completed,
        )

    @classmethod
    def _complete_from_evidence(
        cls,
        session: Session,
        run: EvaluationRun,
        now: datetime,
    ) -> bool:
        """Finalize a complete response set without another provider attempt."""

        if aggregate_run_evidence(session, run) != run.total_questions:
            return False
        run.status = RunStatus.COMPLETED
        run.finished_at = now
        run.next_attempt_at = None
        run.error_message = None
        run.dead_lettered_at = None
        cls._clear_lease(run)
        return True

    @staticmethod
    def _clear_lease(run: EvaluationRun) -> None:
        run.lease_owner = None
        run.lease_expires_at = None
        run.heartbeat_at = None
