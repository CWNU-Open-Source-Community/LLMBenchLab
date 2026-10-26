"""Database-backed Run leases, fencing, retries, cancellation, and response idempotency."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.governance import (
    GovernanceIntegrityError,
    GovernanceRepository,
    append_audit_event,
    record_governance_integrity_event,
)
from app.models import (
    EvaluationResponse,
    EvaluationRun,
    GovernanceRunStatus,
    QuestionExecution,
    RunStatus,
)
from app.services.run_evidence import canonical_run_evidence

DatabaseClock = Callable[[Session], datetime]
_GOVERNANCE_INTEGRITY_REASON = "governance_integrity_error"


class AttemptDisposition(StrEnum):
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    RECOVERED_COMPLETED = "recovered_completed"
    CANCELLED = "cancelled"
    FENCE_LOST = "fence_lost"
    GOVERNANCE_DEFERRED = "governance_deferred"
    GOVERNANCE_EXHAUSTED = "governance_exhausted"
    COOPERATIVE_YIELD = "cooperative_yield"


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
    retry_scheduled: int = 0


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
    completed_response_count = int(response_count or 0)
    metrics = canonical_run_evidence(
        planned_questions=run.total_questions,
        response_count=completed_response_count,
        score_sum=float(score_sum or 0),
        completed_outputs=int(completed_outputs or 0),
        evaluable_responses=int(evaluable or 0),
        error_responses=int(errors or 0),
        average_latency_ms=float(avg_latency) if avg_latency is not None else None,
        known_input_tokens=int(in_tok or 0),
        input_token_reports=int(in_reports or 0),
        known_output_tokens=int(out_tok or 0),
        output_token_reports=int(out_reports or 0),
        known_estimated_cost=Decimal(cost or 0),
        estimated_cost_reports=int(cost_reports or 0),
    )
    run.completed_questions = metrics.completed_questions
    run.correct_questions = metrics.correct_questions
    run.error_questions = metrics.error_questions
    run.score = metrics.score
    run.completion_rate = metrics.completion_rate
    run.answered_accuracy = metrics.answered_accuracy
    run.average_latency_ms = metrics.average_latency_ms
    run.input_tokens = metrics.input_tokens
    run.output_tokens = metrics.output_tokens
    run.estimated_cost = metrics.estimated_cost
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
        self._governance = GovernanceRepository(session_factory, clock=clock)

    def _reconcile_lease_or_raise(
        self,
        *,
        run_id: str,
        lease_token: int,
        model_id: str | None = None,
        worker_id: str | None = None,
    ) -> tuple[int, int]:
        """Record fixed integrity evidence and preserve the control signal."""

        try:
            return self._governance.reconcile_run_lease(
                run_id=run_id,
                lease_token=lease_token,
            )
        except GovernanceIntegrityError:
            record_governance_integrity_event(
                self._session_factory,
                run_id=run_id,
                model_id=model_id,
                worker_id=worker_id,
            )
            raise

    def _fail_closed_takeover(self, lease: RunLease) -> None:
        """Revoke a newly issued takeover lease after old-ledger corruption."""

        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            run = session.scalar(
                select(EvaluationRun)
                .where(
                    EvaluationRun.id == lease.run_id,
                    EvaluationRun.status == RunStatus.RUNNING,
                    EvaluationRun.lease_owner == lease.owner,
                    EvaluationRun.lease_token == lease.token,
                )
                .with_for_update()
            )
            if run is None:
                return
            aggregate_run_evidence(session, run)
            actual_status = RunStatus.CANCELLED if run.cancellation_requested else RunStatus.FAILED
            run.status = actual_status
            if actual_status == RunStatus.FAILED and run.governance_policy_id is not None:
                run.governance_status = GovernanceRunStatus.EXHAUSTED
            run.governance_reason = _GOVERNANCE_INTEGRITY_REASON
            run.governance_not_before = None
            run.last_error = _GOVERNANCE_INTEGRITY_REASON
            run.error_message = (
                _GOVERNANCE_INTEGRITY_REASON if actual_status == RunStatus.FAILED else None
            )
            run.finished_at = now
            run.next_attempt_at = None
            self._clear_lease(run)
            append_audit_event(
                session,
                event_key=f"run:{run.id}:terminal:{actual_status.value}",
                event_type="run_terminal",
                occurred_at=now,
                payload={
                    "status": actual_status.value,
                    "reason": _GOVERNANCE_INTEGRITY_REASON,
                },
                correlation_id=run.id,
                run_id=run.id,
                model_id=run.model_id,
                worker_id=lease.owner,
                attempt=lease.attempt,
                lease_token=lease.token,
            )

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
            & (EvaluationRun.failed_attempt_count < EvaluationRun.max_attempts)
            & (EvaluationRun.governance_status != GovernanceRunStatus.EXHAUSTED)
            & (response_count < EvaluationRun.total_questions)
            & or_(
                (EvaluationRun.status == RunStatus.PENDING)
                & or_(
                    EvaluationRun.next_attempt_at.is_(None),
                    EvaluationRun.next_attempt_at <= now,
                )
                & or_(
                    EvaluationRun.governance_not_before.is_(None),
                    EvaluationRun.governance_not_before <= now,
                ),
                (EvaluationRun.status == RunStatus.RUNNING)
                & EvaluationRun.lease_expires_at.is_not(None)
                & (EvaluationRun.lease_expires_at <= now)
                & (EvaluationRun.failed_attempt_count + 1 < EvaluationRun.max_attempts),
            )
        )

    def claim(self, run_id: str, *, owner: str) -> RunLease | None:
        if not owner or len(owner) > 128:
            raise ValueError("owner must contain 1 to 128 characters")
        expired_lease_token: int | None = None
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            expires_at = now + self._lease_for
            current = session.execute(
                select(
                    EvaluationRun.status,
                    EvaluationRun.lease_expires_at,
                    EvaluationRun.lease_token,
                )
                .where(EvaluationRun.id == run_id)
                .with_for_update()
            ).one_or_none()
            taking_expired_lease = bool(
                current is not None
                and current.status == RunStatus.RUNNING
                and current.lease_expires_at is not None
                and _as_utc(current.lease_expires_at) <= now
            )
            if taking_expired_lease and current is not None:
                expired_lease_token = int(current.lease_token)
            was_expired = (
                (EvaluationRun.status == RunStatus.RUNNING)
                & EvaluationRun.lease_expires_at.is_not(None)
                & (EvaluationRun.lease_expires_at <= now)
            )
            result = session.execute(
                update(EvaluationRun)
                .where(EvaluationRun.id == run_id, self._eligible(now))
                .values(
                    status=RunStatus.RUNNING,
                    attempt_count=EvaluationRun.attempt_count + 1,
                    failed_attempt_count=EvaluationRun.failed_attempt_count
                    + case((was_expired, 1), else_=0),
                    dispatch_count=EvaluationRun.dispatch_count + 1,
                    last_scheduled_at=now,
                    lease_owner=owner,
                    lease_token=EvaluationRun.lease_token + 1,
                    lease_expires_at=expires_at,
                    heartbeat_at=now,
                    next_attempt_at=None,
                    governance_status=case(
                        (
                            EvaluationRun.governance_status == GovernanceRunStatus.DELAYED,
                            GovernanceRunStatus.MANAGED,
                        ),
                        else_=EvaluationRun.governance_status,
                    ),
                    governance_reason=None,
                    governance_not_before=None,
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
            if taking_expired_lease:
                self._begin_new_execution_generation(session, run_id)
            row = session.execute(
                select(
                    EvaluationRun.lease_token,
                    EvaluationRun.attempt_count,
                    EvaluationRun.lease_expires_at,
                    EvaluationRun.dispatch_count,
                    EvaluationRun.model_id,
                ).where(EvaluationRun.id == run_id, EvaluationRun.lease_owner == owner)
            ).one()
            append_audit_event(
                session,
                event_key=f"run:{run_id}:lease:{int(row.lease_token)}:claimed",
                event_type="run_claimed",
                occurred_at=now,
                payload={"dispatch_count": int(row.dispatch_count)},
                correlation_id=run_id,
                run_id=run_id,
                model_id=row.model_id,
                worker_id=owner,
                attempt=int(row.attempt_count),
                lease_token=int(row.lease_token),
            )
            lease = RunLease(
                run_id=run_id,
                owner=owner,
                token=int(row.lease_token),
                attempt=int(row.attempt_count),
                expires_at=_as_utc(row.lease_expires_at),
            )
        if expired_lease_token is not None:
            try:
                self._governance.reconcile_run_lease(
                    run_id=run_id,
                    lease_token=expired_lease_token,
                )
            except GovernanceIntegrityError:
                self._fail_closed_takeover(lease)
                record_governance_integrity_event(
                    self._session_factory,
                    run_id=run_id,
                    model_id=row.model_id,
                    worker_id=owner,
                )
                raise
        return lease

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
                    .order_by(
                        expired_first,
                        func.coalesce(
                            EvaluationRun.last_scheduled_at,
                            EvaluationRun.created_at,
                        ),
                        EvaluationRun.created_at,
                        EvaluationRun.id,
                    )
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
        expires_at = self._governance.renew_run_lease(
            run_id=lease.run_id,
            lease_owner=lease.owner,
            lease_token=lease.token,
            lease_for=self._lease_for,
        )
        if expires_at is None:
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
                if response.error_type is None:
                    error_category = "none"
                elif response.error_type in {"parse_error", "output_truncated"}:
                    error_category = "parse_error"
                elif response.error_type == "evaluator_internal_error":
                    error_category = "evaluator_error"
                elif response.error_type == "question_internal_error":
                    error_category = "internal_error"
                else:
                    error_category = "adapter_error"
                append_audit_event(
                    session,
                    event_key=f"response:{response.id}:persisted",
                    event_type="question_evidence_persisted",
                    occurred_at=now,
                    payload={"error_code": error_category},
                    correlation_id=lease.run_id,
                    run_id=lease.run_id,
                    question_id=response.question_id,
                    worker_id=lease.owner,
                    attempt=lease.attempt,
                    lease_token=lease.token,
                    duration_ms=(
                        float(response.latency_ms) if response.latency_ms is not None else None
                    ),
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
                append_audit_event(
                    session,
                    event_key=f"run:{run.id}:cancel_requested",
                    event_type="run_cancel_requested",
                    occurred_at=now,
                    correlation_id=run.id,
                    run_id=run.id,
                    model_id=run.model_id,
                )
                aggregate_run_evidence(session, run)
                run.status = RunStatus.CANCELLED
                run.finished_at = now
                run.next_attempt_at = None
                append_audit_event(
                    session,
                    event_key=f"run:{run.id}:terminal:cancelled",
                    event_type="run_terminal",
                    occurred_at=now,
                    payload={"status": "cancelled", "reason": "none"},
                    correlation_id=run.id,
                    run_id=run.id,
                    model_id=run.model_id,
                )
                return CancelDisposition.CANCELLED
            if active_status == RunStatus.RUNNING:
                run = session.get(EvaluationRun, run_id)
                if run is None:
                    raise RuntimeError("cancelled_run_missing")
                append_audit_event(
                    session,
                    event_key=f"run:{run.id}:lease:{run.lease_token}:cancel_requested",
                    event_type="run_cancel_requested",
                    occurred_at=now,
                    correlation_id=run.id,
                    run_id=run.id,
                    model_id=run.model_id,
                    worker_id=run.lease_owner,
                    attempt=run.attempt_count,
                    lease_token=run.lease_token,
                )
                return CancelDisposition.REQUESTED
            status_value = session.scalar(
                select(EvaluationRun.status).where(EvaluationRun.id == run_id)
            )
            return (
                CancelDisposition.NOT_FOUND if status_value is None else CancelDisposition.TERMINAL
            )

    def finish_cancelled(self, lease: RunLease) -> bool:
        transitioned = False
        model_id: str | None = None
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            run = self._locked_run(session, lease, now)
            if run is not None:
                model_id = run.model_id
            if run is not None and run.cancellation_requested:
                aggregate_run_evidence(session, run)
                run.status = RunStatus.CANCELLED
                run.finished_at = now
                self._clear_lease(run)
                append_audit_event(
                    session,
                    event_key=f"run:{run.id}:terminal:cancelled",
                    event_type="run_terminal",
                    occurred_at=now,
                    payload={"status": "cancelled", "reason": "none"},
                    correlation_id=run.id,
                    run_id=run.id,
                    model_id=run.model_id,
                    worker_id=lease.owner,
                    attempt=lease.attempt,
                    lease_token=lease.token,
                )
                transitioned = True
        self._reconcile_lease_or_raise(
            run_id=lease.run_id,
            lease_token=lease.token,
            model_id=model_id,
            worker_id=lease.owner,
        )
        return transitioned

    def record_notification_result(self, run_id: str, *, published: bool) -> bool:
        """Record best-effort queue notification evidence without owning task state."""

        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            values: dict[str, object] = {
                "last_error": None if published else "queue_notification_unavailable"
            }
            if published:
                values["last_enqueued_at"] = now
            result = session.execute(
                update(EvaluationRun)
                .where(
                    EvaluationRun.id == run_id,
                    EvaluationRun.status == RunStatus.PENDING,
                    EvaluationRun.attempt_count == 0,
                )
                .values(**values)
            )
            if result.rowcount == 1:
                run = session.get(EvaluationRun, run_id)
                if run is not None:
                    append_audit_event(
                        session,
                        event_key=(
                            f"run:{run_id}:queue_notification:"
                            f"{'published' if published else 'unavailable'}"
                        ),
                        event_type="queue_notification",
                        occurred_at=now,
                        payload={"result": "published" if published else "unavailable"},
                        correlation_id=run_id,
                        run_id=run_id,
                        model_id=run.model_id,
                    )
            return result.rowcount == 1

    def fail_attempt(self, lease: RunLease, *, error_code: str) -> AttemptDisposition:
        safe_error = _safe_error_code(error_code)
        disposition: AttemptDisposition
        model_id: str
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            run = self._locked_run(session, lease, now)
            if run is None:
                return AttemptDisposition.FENCE_LOST
            model_id = run.model_id
            if run.cancellation_requested:
                aggregate_run_evidence(session, run)
                run.status = RunStatus.CANCELLED
                run.finished_at = now
                self._clear_lease(run)
                disposition = AttemptDisposition.CANCELLED
            else:
                run.last_error = safe_error
                completed_response_count = aggregate_run_evidence(session, run)
                if self._complete_from_evidence(run, now, completed_response_count):
                    disposition = AttemptDisposition.RECOVERED_COMPLETED
                else:
                    run.failed_attempt_count += 1
                    if run.failed_attempt_count >= run.max_attempts:
                        run.status = RunStatus.FAILED
                        run.dead_lettered_at = now
                        run.finished_at = now
                        run.error_message = safe_error
                        self._clear_lease(run)
                        disposition = AttemptDisposition.DEAD_LETTERED
                    else:
                        exponent = max(run.failed_attempt_count - 1, 0)
                        delay_seconds = min(
                            self._retry_backoff_base.total_seconds() * (2**exponent),
                            self._retry_backoff_cap.total_seconds(),
                        )
                        run.status = RunStatus.PENDING
                        run.next_attempt_at = now + timedelta(seconds=delay_seconds)
                        self._begin_new_execution_generation(session, run.id)
                        self._clear_lease(run)
                        disposition = AttemptDisposition.RETRY_SCHEDULED
            if disposition == AttemptDisposition.RETRY_SCHEDULED:
                append_audit_event(
                    session,
                    event_key=(
                        f"run:{run.id}:failed_attempt:{run.failed_attempt_count}:retry_scheduled"
                    ),
                    event_type="run_retry_scheduled",
                    occurred_at=now,
                    payload={
                        "failed_attempt_count": run.failed_attempt_count,
                        "reason": "worker_error",
                    },
                    correlation_id=run.id,
                    run_id=run.id,
                    model_id=run.model_id,
                    worker_id=lease.owner,
                    attempt=lease.attempt,
                    lease_token=lease.token,
                )
            elif disposition == AttemptDisposition.DEAD_LETTERED:
                append_audit_event(
                    session,
                    event_key=f"run:{run.id}:dead_lettered",
                    event_type="run_dead_lettered",
                    occurred_at=now,
                    payload={
                        "failed_attempt_count": run.failed_attempt_count,
                        "reason": "worker_error",
                    },
                    correlation_id=run.id,
                    run_id=run.id,
                    model_id=run.model_id,
                    worker_id=lease.owner,
                    attempt=lease.attempt,
                    lease_token=lease.token,
                )
            if disposition in {
                AttemptDisposition.CANCELLED,
                AttemptDisposition.RECOVERED_COMPLETED,
                AttemptDisposition.DEAD_LETTERED,
            }:
                status_value = run.status.value
                append_audit_event(
                    session,
                    event_key=f"run:{run.id}:terminal:{status_value}",
                    event_type="run_terminal",
                    occurred_at=now,
                    payload={"status": status_value, "reason": "worker_error"},
                    correlation_id=run.id,
                    run_id=run.id,
                    model_id=run.model_id,
                    worker_id=lease.owner,
                    attempt=lease.attempt,
                    lease_token=lease.token,
                )
        self._reconcile_lease_or_raise(
            run_id=lease.run_id,
            lease_token=lease.token,
            model_id=model_id,
            worker_id=lease.owner,
        )
        return disposition

    def defer_governance(
        self,
        lease: RunLease,
        *,
        reason: str,
        not_before: datetime,
    ) -> AttemptDisposition:
        """Cooperatively release a lease under transient governance pressure."""

        safe_reason = _safe_error_code(reason)
        model_id: str
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            run = self._locked_run(session, lease, now)
            if run is None:
                return AttemptDisposition.FENCE_LOST
            model_id = run.model_id
            run.status = RunStatus.PENDING
            run.governance_status = GovernanceRunStatus.DELAYED
            run.governance_reason = safe_reason
            run.governance_not_before = _as_utc(not_before)
            run.next_attempt_at = None
            self._clear_lease(run)
            append_audit_event(
                session,
                event_key=f"run:{run.id}:lease:{lease.token}:deferred",
                event_type="run_deferred",
                occurred_at=now,
                payload={
                    "reason": "governance_deferred",
                    "not_before": _as_utc(not_before),
                },
                correlation_id=run.id,
                run_id=run.id,
                model_id=run.model_id,
                worker_id=lease.owner,
                attempt=lease.attempt,
                lease_token=lease.token,
            )
        self._reconcile_lease_or_raise(
            run_id=lease.run_id,
            lease_token=lease.token,
            model_id=model_id,
            worker_id=lease.owner,
        )
        return AttemptDisposition.GOVERNANCE_DEFERRED

    def cooperative_yield(
        self,
        lease: RunLease,
        *,
        responses_added: int,
    ) -> AttemptDisposition:
        """Release a healthy managed Run after one bounded question quantum."""

        if responses_added < 0:
            raise ValueError("responses_added must be non-negative")
        model_id: str
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            run = self._locked_run(session, lease, now)
            if run is None:
                return AttemptDisposition.FENCE_LOST
            model_id = run.model_id
            run.status = RunStatus.PENDING
            run.governance_status = GovernanceRunStatus.MANAGED
            run.governance_reason = None
            run.governance_not_before = None
            run.next_attempt_at = None
            run.last_scheduled_at = now
            self._clear_lease(run)
            append_audit_event(
                session,
                event_key=f"run:{run.id}:lease:{lease.token}:yielded",
                event_type="run_yielded",
                occurred_at=now,
                payload={"responses_added": responses_added},
                correlation_id=run.id,
                run_id=run.id,
                model_id=run.model_id,
                worker_id=lease.owner,
                attempt=lease.attempt,
                lease_token=lease.token,
            )
        self._reconcile_lease_or_raise(
            run_id=lease.run_id,
            lease_token=lease.token,
            model_id=model_id,
            worker_id=lease.owner,
        )
        return AttemptDisposition.COOPERATIVE_YIELD

    def exhaust_governance(
        self,
        lease: RunLease,
        *,
        reason: str,
        integrity_error: bool = False,
    ) -> AttemptDisposition:
        """Fail a Run without consuming Worker retry budget when policy is permanent."""

        safe_reason = _safe_error_code(reason)
        model_id: str
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            run = self._locked_run(session, lease, now)
            if run is None:
                return AttemptDisposition.FENCE_LOST
            model_id = run.model_id
            aggregate_run_evidence(session, run)
            run.status = RunStatus.FAILED
            run.governance_status = GovernanceRunStatus.EXHAUSTED
            run.governance_reason = safe_reason
            run.governance_not_before = None
            run.last_error = safe_reason
            run.error_message = safe_reason
            run.finished_at = now
            run.next_attempt_at = None
            self._clear_lease(run)
            if integrity_error:
                append_audit_event(
                    session,
                    event_key=f"run:{run.id}:lease:{lease.token}:governance-integrity",
                    event_type="governance_integrity_error",
                    occurred_at=now,
                    payload={"reason": "governance_integrity_error"},
                    correlation_id=run.id,
                    run_id=run.id,
                    model_id=run.model_id,
                    worker_id=lease.owner,
                    attempt=lease.attempt,
                    lease_token=lease.token,
                )
            append_audit_event(
                session,
                event_key=f"run:{run.id}:terminal:failed",
                event_type="run_terminal",
                occurred_at=now,
                payload={
                    "status": "failed",
                    "reason": (
                        "governance_integrity_error" if integrity_error else "governance_exhausted"
                    ),
                },
                correlation_id=run.id,
                run_id=run.id,
                model_id=run.model_id,
                worker_id=lease.owner,
                attempt=lease.attempt,
                lease_token=lease.token,
            )
        self._reconcile_lease_or_raise(
            run_id=lease.run_id,
            lease_token=lease.token,
            model_id=model_id,
            worker_id=lease.owner,
        )
        return AttemptDisposition.GOVERNANCE_EXHAUSTED

    def reap_expired(self, *, limit: int = 100) -> ReapReport:
        if limit < 1:
            return ReapReport()
        with self._session_factory() as scan_session:
            scan_now = self._clock(scan_session)
            expired_rows = tuple(
                scan_session.execute(
                    select(EvaluationRun.id, EvaluationRun.lease_token)
                    .where(
                        EvaluationRun.status == RunStatus.RUNNING,
                        EvaluationRun.lease_expires_at <= scan_now,
                    )
                    .order_by(EvaluationRun.lease_expires_at, EvaluationRun.id)
                    .limit(limit)
                )
            )
        for row in expired_rows:
            self._governance.reconcile_run_lease(
                run_id=row.id,
                lease_token=int(row.lease_token),
            )
        cancelled = 0
        dead_lettered = 0
        completed = 0
        retry_scheduled = 0
        with self._session_factory() as session, session.begin():
            _acquire_sqlite_transition_lock(session)
            now = self._clock(session)
            for expired_row in expired_rows:
                run = session.scalar(
                    select(EvaluationRun)
                    .where(EvaluationRun.id == expired_row.id)
                    .with_for_update()
                )
                if (
                    run is None
                    or run.status != RunStatus.RUNNING
                    or run.lease_token != int(expired_row.lease_token)
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
                    append_audit_event(
                        session,
                        event_key=f"run:{run.id}:terminal:cancelled",
                        event_type="run_terminal",
                        occurred_at=now,
                        payload={"status": "cancelled", "reason": "lease_expired"},
                        correlation_id=run.id,
                        run_id=run.id,
                        model_id=run.model_id,
                        attempt=run.attempt_count,
                        lease_token=int(expired_row.lease_token),
                    )
                else:
                    completed_response_count = aggregate_run_evidence(session, run)
                    if self._complete_from_evidence(run, now, completed_response_count):
                        completed += 1
                        append_audit_event(
                            session,
                            event_key=f"run:{run.id}:terminal:completed",
                            event_type="run_terminal",
                            occurred_at=now,
                            payload={"status": "completed", "reason": "lease_expired"},
                            correlation_id=run.id,
                            run_id=run.id,
                            model_id=run.model_id,
                            attempt=run.attempt_count,
                            lease_token=int(expired_row.lease_token),
                        )
                    else:
                        run.failed_attempt_count += 1
                        error_code = "worker_lease_expired"
                        run.last_error = error_code
                        if run.failed_attempt_count >= run.max_attempts:
                            run.status = RunStatus.FAILED
                            run.error_message = "worker_lease_expired_retry_exhausted"
                            run.dead_lettered_at = now
                            run.finished_at = now
                            self._clear_lease(run)
                            dead_lettered += 1
                            append_audit_event(
                                session,
                                event_key=f"run:{run.id}:dead_lettered",
                                event_type="run_dead_lettered",
                                occurred_at=now,
                                payload={
                                    "failed_attempt_count": run.failed_attempt_count,
                                    "reason": "lease_expired",
                                },
                                correlation_id=run.id,
                                run_id=run.id,
                                model_id=run.model_id,
                                attempt=run.attempt_count,
                                lease_token=int(expired_row.lease_token),
                            )
                            append_audit_event(
                                session,
                                event_key=f"run:{run.id}:terminal:failed",
                                event_type="run_terminal",
                                occurred_at=now,
                                payload={"status": "failed", "reason": "lease_expired"},
                                correlation_id=run.id,
                                run_id=run.id,
                                model_id=run.model_id,
                                attempt=run.attempt_count,
                                lease_token=int(expired_row.lease_token),
                            )
                        else:
                            exponent = max(run.failed_attempt_count - 1, 0)
                            delay_seconds = min(
                                self._retry_backoff_base.total_seconds() * (2**exponent),
                                self._retry_backoff_cap.total_seconds(),
                            )
                            run.status = RunStatus.PENDING
                            run.next_attempt_at = now + timedelta(seconds=delay_seconds)
                            self._begin_new_execution_generation(session, run.id)
                            self._clear_lease(run)
                            retry_scheduled += 1
                            append_audit_event(
                                session,
                                event_key=(
                                    f"run:{run.id}:failed_attempt:"
                                    f"{run.failed_attempt_count}:retry_scheduled"
                                ),
                                event_type="run_retry_scheduled",
                                occurred_at=now,
                                payload={
                                    "failed_attempt_count": run.failed_attempt_count,
                                    "reason": "lease_expired",
                                },
                                correlation_id=run.id,
                                run_id=run.id,
                                model_id=run.model_id,
                                attempt=run.attempt_count,
                                lease_token=int(expired_row.lease_token),
                            )
        return ReapReport(
            cancelled=cancelled,
            dead_lettered=dead_lettered,
            completed=completed,
            retry_scheduled=retry_scheduled,
        )

    @classmethod
    def _complete_from_evidence(
        cls,
        run: EvaluationRun,
        now: datetime,
        completed_response_count: int,
    ) -> bool:
        """Finalize a complete response set without another provider attempt."""

        if completed_response_count != run.total_questions:
            return False
        run.status = RunStatus.COMPLETED
        run.finished_at = now
        run.next_attempt_at = None
        run.error_message = None
        run.dead_lettered_at = None
        cls._clear_lease(run)
        return True

    @staticmethod
    def _begin_new_execution_generation(session: Session, run_id: str) -> None:
        """Reset only unfinished question retry cursors after a true Run failure."""

        completed_question_ids = select(EvaluationResponse.question_id).where(
            EvaluationResponse.run_id == run_id
        )
        session.execute(
            update(QuestionExecution)
            .where(
                QuestionExecution.run_id == run_id,
                QuestionExecution.question_id.not_in(completed_question_ids),
            )
            .values(
                execution_generation=QuestionExecution.execution_generation + 1,
                next_provider_attempt=1,
                first_attempt_at=None,
                retry_not_before=None,
            )
        )

    @staticmethod
    def _clear_lease(run: EvaluationRun) -> None:
        run.lease_owner = None
        run.lease_expires_at = None
        run.heartbeat_at = None
