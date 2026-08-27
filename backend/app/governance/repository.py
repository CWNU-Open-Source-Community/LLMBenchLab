"""Database-authoritative admission, attempt accounting, and reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select, union_all
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.constants import MAX_GOVERNANCE_COST_USD
from app.models import (
    EvaluationRun,
    GovernanceMinuteBucket,
    GovernancePolicy,
    GovernanceRunStatus,
    GovernanceScope,
    GovernanceScopeType,
    ProviderCallReservation,
    ProviderCallReservationState,
    QuestionExecution,
    RunStatus,
)
from app.provider_attempts import (
    ProviderAttemptContext,
    ProviderAttemptController,
    ProviderAttemptDisposition,
    ProviderAttemptOutcome,
    ProviderAttemptPermit,
    ProviderAttemptStateUnknown,
)
from app.security.credentials import CredentialInputError, normalize_provider_origin

from .audit import append_audit_event

_DEFAULT_POLICY_ID = "00000000-0000-0000-0000-000000000009"
_MONEY_QUANTUM = Decimal("0.00000001")
_MONEY_MAX = MAX_GOVERNANCE_COST_USD
_INT32_MAX = 2**31 - 1
_INT64_MAX = 2**63 - 1
_SCOPE_ORDER = {
    GovernanceScopeType.GLOBAL: 0,
    GovernanceScopeType.PROVIDER: 1,
    GovernanceScopeType.MODEL: 2,
    GovernanceScopeType.RUN: 3,
}
_ACTIVE_STATES = (
    ProviderCallReservationState.RESERVED,
    ProviderCallReservationState.SEND_STARTED,
)
_SETTLED_STATES = (
    ProviderCallReservationState.SETTLED_ACTUAL,
    ProviderCallReservationState.SETTLED_CONSERVATIVE,
)
_CONSUMED_REQUEST_STATES = (
    ProviderCallReservationState.SEND_STARTED,
    *_SETTLED_STATES,
)
_POLICY_FIELDS = (
    "global_concurrency_limit",
    "provider_concurrency_limit",
    "model_concurrency_limit",
    "run_concurrency_limit",
    "global_requests_per_minute",
    "provider_requests_per_minute",
    "model_requests_per_minute",
    "run_requests_per_minute",
    "global_tokens_per_minute",
    "provider_tokens_per_minute",
    "model_tokens_per_minute",
    "run_tokens_per_minute",
    "global_lifetime_request_budget",
    "global_lifetime_token_budget",
    "global_lifetime_cost_budget_usd",
    "run_lifetime_request_budget",
    "run_lifetime_token_budget",
    "run_lifetime_cost_budget_usd",
    "backlog_limit",
    "question_quantum",
)
_POLICY_COST_FIELDS = frozenset(
    {
        "global_lifetime_cost_budget_usd",
        "run_lifetime_cost_budget_usd",
    }
)
_POLICY_INT32_FIELDS = frozenset(
    {
        "global_concurrency_limit",
        "provider_concurrency_limit",
        "model_concurrency_limit",
        "run_concurrency_limit",
        "backlog_limit",
        "question_quantum",
    }
)


class GovernanceControlSignal(RuntimeError):
    """Base class for Runner control flow that must never become a zero Response."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GovernanceBacklogFull(GovernanceControlSignal):
    """A new Run cannot be admitted under the active finite backlog policy."""

    def __init__(self, *, limit: int) -> None:
        super().__init__("run_backlog_full")
        self.limit = limit


class GovernanceDeferred(GovernanceControlSignal):
    """Transient capacity/rate pressure; the Run should cooperatively yield."""

    def __init__(self, code: str, *, not_before: datetime) -> None:
        super().__init__(code)
        self.not_before = not_before


class GovernanceExhausted(GovernanceControlSignal):
    """A deterministic policy/budget condition prevents further Provider calls."""


class GovernanceFenceLost(GovernanceControlSignal):
    """The Worker lease no longer authorizes a new Provider attempt."""


class GovernanceSettlementUnknown(GovernanceControlSignal, ProviderAttemptStateUnknown):
    """A database acknowledgement was uncertain; no further retry may be sent."""

    def __init__(self) -> None:
        super().__init__("governance_settlement_unknown")


class GovernanceIntegrityError(GovernanceControlSignal):
    """Ledger/counter facts disagree and admission must fail closed."""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _database_clock(session: Session) -> datetime:
    value = session.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("database_clock_unavailable")
    return _as_utc(value)


def _minute_start(value: datetime) -> datetime:
    return _as_utc(value).replace(second=0, microsecond=0)


def _money(value: Decimal | str | int | None) -> Decimal:
    if value is None:
        return Decimal(0)
    return Decimal(str(value)).quantize(_MONEY_QUANTUM, rounding=ROUND_CEILING)


def _nonnegative_money(name: str, value: Decimal | str | int) -> Decimal:
    """Validate a public monetary input before fixed-scale ceiling rounding."""

    try:
        raw = Decimal(str(value))
        if not raw.is_finite() or raw < 0:
            raise ValueError
        normalized = raw.quantize(_MONEY_QUANTUM, rounding=ROUND_CEILING)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite non-negative USD amount") from None
    if normalized > _MONEY_MAX:
        raise ValueError(f"{name} exceeds the supported USD amount")
    return normalized


def _nonnegative_int(
    name: str,
    value: int | None,
    *,
    maximum: int = _INT64_MAX,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be a bounded non-negative integer or null")
    return value


def provider_scope_key(provider_type: str, base_url: str | None) -> str:
    """Return an opaque stable provider identity without persisting an endpoint."""

    provider = provider_type.strip().lower()
    if provider == "mock":
        origin = "offline"
    else:
        if not base_url:
            raise ValueError("provider base URL is required")
        try:
            origin = normalize_provider_origin(base_url)
        except CredentialInputError:
            raise ValueError(
                "provider base URL must contain a safe absolute HTTP(S) origin"
            ) from None
    material = f"{provider}\0{origin}".encode()
    return hashlib.sha256(material).hexdigest()


def _default_policy_values(now: datetime) -> dict[str, Any]:
    limits: dict[str, Any] = {
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
    return {
        "id": _DEFAULT_POLICY_ID,
        "version": 1,
        "policy_hash": _policy_hash(limits),
        "is_active": True,
        **limits,
        "activated_at": now,
        "created_at": now,
    }


def _policy_hash(values: Mapping[str, Any]) -> str:
    canonical = {
        name: (
            format(_money(values[name]), "f")
            if name in _POLICY_COST_FIELDS and values[name] is not None
            else values[name]
        )
        for name in _POLICY_FIELDS
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _begin_sqlite_write(session: Session) -> None:
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _insert_do_nothing(
    session: Session,
    table: Any,
    values: dict[str, Any],
    *,
    conflict_columns: Sequence[str],
) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = (
            postgresql_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(conflict_columns))
        )
    elif dialect == "sqlite":
        statement = (
            sqlite_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(conflict_columns))
        )
    else:  # The supported production/development dialects are handled above.
        if (
            session.execute(
                select(table).where(
                    *(table.c[column] == values[column] for column in conflict_columns)
                )
            ).first()
            is not None
        ):
            return
        statement = table.insert().values(**values)
    session.execute(statement)


def _policy_snapshot(policy: GovernancePolicy) -> dict[str, Any]:
    values: dict[str, Any] = {
        "policy_id": policy.id,
        "policy_version": policy.version,
        "policy_hash": policy.policy_hash,
    }
    for name in _POLICY_FIELDS:
        value = getattr(policy, name)
        values[name] = str(value) if isinstance(value, Decimal) else value
    return values


def _validate_policy_integrity(policy: GovernancePolicy) -> None:
    values = {name: getattr(policy, name) for name in _POLICY_FIELDS}
    if _policy_hash(values) != policy.policy_hash:
        raise GovernanceIntegrityError("governance_policy_hash_mismatch")


def _run_override_snapshot(run: EvaluationRun) -> dict[str, Any]:
    """Return the canonical, secret-free Run override document frozen at admission."""

    return {
        "input_token_reservation": run.input_token_reservation,
        "lifetime_request_budget": run.lifetime_request_budget,
        "lifetime_token_budget": run.lifetime_token_budget,
        "lifetime_cost_budget_usd": (
            format(_money(run.lifetime_cost_budget_usd), "f")
            if run.lifetime_cost_budget_usd is not None
            else None
        ),
    }


def _normalize_policy_values(values: Mapping[str, Any]) -> dict[str, Any]:
    if set(values) != set(_POLICY_FIELDS):
        raise ValueError("governance policy must provide the complete supported field set")
    normalized: dict[str, Any] = {}
    for name in _POLICY_FIELDS:
        value = values[name]
        if name in _POLICY_COST_FIELDS:
            if value is None:
                normalized[name] = None
                continue
            normalized[name] = _nonnegative_money(name, value)
            continue
        if value is None:
            if name in {"backlog_limit", "question_quantum"}:
                raise ValueError(f"{name} cannot be null")
            normalized[name] = None
            continue
        maximum = _INT32_MAX if name in _POLICY_INT32_FIELDS else _INT64_MAX
        if isinstance(value, bool) or not isinstance(value, int) or value > maximum:
            raise ValueError(f"{name} must be an integer or null")
        minimum = 1 if name == "question_quantum" else 0
        if value < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
        normalized[name] = value
    return normalized


class GovernanceRepository:
    """Coordinate governance exclusively through short database transactions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Any = _database_clock,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def lock_run_admission_scope(self, session: Session) -> None:
        """Establish PostgreSQL's global-scope-before-Model lock order.

        Run creation also locks the Model row so endpoint or credential changes
        cannot race admission. Provider reservation takes governance scopes
        before inserting a ledger row whose Model foreign key needs a key-share
        lock. Pre-locking the global scope here prevents the inverse
        Model-to-global order from deadlocking those transactions. SQLite keeps
        using the Model helper's database-wide ``BEGIN IMMEDIATE`` serialization.
        ``admit_run`` re-locks and validates this same scope later in the
        transaction.
        """

        if session.get_bind().dialect.name != "postgresql":
            return
        now = self._clock(session)
        self._lock_scopes(
            session,
            ((GovernanceScopeType.GLOBAL, "global"),),
            now=now,
        )

    def ensure_default_policy(self, session: Session) -> GovernancePolicy:
        """Return the active policy, bootstrapping a deterministic unlimited v1."""

        _begin_sqlite_write(session)
        return self._ensure_default_policy_locked(session)

    def _ensure_default_policy_locked(self, session: Session) -> GovernancePolicy:
        """Return/bootstrap the policy after the caller serialized SQLite writes."""

        active_rows = list(
            session.scalars(
                select(GovernancePolicy)
                .where(GovernancePolicy.is_active.is_(True))
                .order_by(GovernancePolicy.version)
                .limit(2)
                .with_for_update()
            )
        )
        if len(active_rows) > 1:
            raise GovernanceIntegrityError("governance_multiple_active_policies")
        if active_rows:
            _validate_policy_integrity(active_rows[0])
            return active_rows[0]
        now = self._clock(session)
        values = _default_policy_values(now)
        _insert_do_nothing(
            session,
            GovernancePolicy.__table__,
            values,
            conflict_columns=("id",),
        )
        active_rows = list(
            session.scalars(
                select(GovernancePolicy)
                .where(GovernancePolicy.is_active.is_(True))
                .order_by(GovernancePolicy.version)
                .limit(2)
                .with_for_update()
            )
        )
        if len(active_rows) > 1:
            raise GovernanceIntegrityError("governance_multiple_active_policies")
        if not active_rows:
            raise GovernanceIntegrityError("governance_active_policy_missing")
        active = active_rows[0]
        _validate_policy_integrity(active)
        append_audit_event(
            session,
            event_key=f"policy:{active.id}:bootstrapped",
            event_type="governance_policy_bootstrapped",
            occurred_at=now,
            payload={"policy_version": active.version, "policy_hash": active.policy_hash},
        )
        return active

    def active_policy(self, session: Session) -> GovernancePolicy | None:
        """Read the single active policy without bootstrapping or taking write locks."""

        active_rows = list(
            session.scalars(
                select(GovernancePolicy)
                .where(GovernancePolicy.is_active.is_(True))
                .order_by(GovernancePolicy.version)
                .limit(2)
            )
        )
        if len(active_rows) > 1:
            raise GovernanceIntegrityError("governance_multiple_active_policies")
        if active_rows:
            _validate_policy_integrity(active_rows[0])
            return active_rows[0]
        return None

    def apply_policy(
        self,
        session: Session,
        values: Mapping[str, Any],
    ) -> GovernancePolicy:
        """Atomically activate an immutable full policy document.

        Applying the current content is idempotent. Reapplying an older content
        hash reactivates that immutable row instead of violating its unique hash.
        """

        normalized = _normalize_policy_values(values)
        policy_hash = _policy_hash(normalized)
        _begin_sqlite_write(session)
        now = self._clock(session)
        self._lock_scopes(
            session,
            ((GovernanceScopeType.GLOBAL, "global"),),
            now=now,
        )
        current = self._ensure_default_policy_locked(session)
        if current.policy_hash == policy_hash:
            return current

        target = session.scalar(
            select(GovernancePolicy)
            .where(GovernancePolicy.policy_hash == policy_hash)
            .with_for_update()
        )
        if target is not None:
            _validate_policy_integrity(target)
        current.is_active = False
        # The partial unique index requires the old active row to become false
        # before either an INSERT or a reactivation UPDATE can make the target true.
        session.flush()
        if target is None:
            next_version = int(session.scalar(select(func.max(GovernancePolicy.version))) or 0) + 1
            target = GovernancePolicy(
                version=next_version,
                policy_hash=policy_hash,
                is_active=True,
                activated_at=now,
                created_at=now,
                **normalized,
            )
            session.add(target)
        else:
            target.is_active = True
            target.activated_at = now
        session.flush()
        append_audit_event(
            session,
            event_key=f"policy:{target.id}:activated:{uuid4()}",
            event_type="governance_policy_applied",
            occurred_at=now,
            payload={"policy_version": target.version, "policy_hash": target.policy_hash},
        )
        return target

    def admit_run(
        self,
        session: Session,
        run: EvaluationRun,
        *,
        provider_type: str,
        base_url: str | None,
    ) -> EvaluationRun:
        """Atomically enforce backlog, freeze policy, and insert a new managed Run."""

        now = self._clock(session)
        global_scope = self._lock_scopes(
            session,
            ((GovernanceScopeType.GLOBAL, "global"),),
            now=now,
        )[0]
        self._validate_scope_materialization_locked(session, (global_scope,))
        policy = self._ensure_default_policy_locked(session)
        backlog_count = int(
            session.scalar(
                select(func.count(EvaluationRun.id)).where(
                    EvaluationRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
                    EvaluationRun.governance_status != GovernanceRunStatus.LEGACY_UNMANAGED,
                )
            )
            or 0
        )
        if backlog_count >= policy.backlog_limit:
            raise GovernanceBacklogFull(limit=policy.backlog_limit)

        opaque_provider = provider_scope_key(provider_type, base_url)
        run.created_at = now
        run.governance_policy_id = policy.id
        run.governance_status = GovernanceRunStatus.MANAGED
        run.governance_reason = None
        run.governance_not_before = None
        snapshot = dict(run.model_parameters_snapshot or {})
        snapshot["governance"] = {
            **_policy_snapshot(policy),
            "provider_scope_key": opaque_provider,
            "local_admission_only": True,
            "run_overrides": _run_override_snapshot(run),
        }
        run.model_parameters_snapshot = snapshot
        session.add(run)
        session.flush()
        append_audit_event(
            session,
            event_key=f"run:{run.id}:admitted",
            event_type="run_admitted",
            occurred_at=now,
            payload={
                "policy_version": policy.version,
                "backlog_count": backlog_count,
                "question_quantum": policy.question_quantum,
            },
            correlation_id=run.id,
            run_id=run.id,
            model_id=run.model_id,
        )
        return run

    def question_context(
        self,
        *,
        run_id: str,
        question_id: str,
        model_id: str,
        provider_scope: str,
        lease_owner: str,
        lease_token: int,
        estimated_input_tokens: int | None,
        reserved_output_tokens: int | None,
        reserved_cost_usd: Decimal | None,
    ) -> ProviderAttemptContext:
        """Load/create the persistent question retry cursor under the current fence."""

        with self._session_factory() as session, session.begin():
            _begin_sqlite_write(session)
            now = self._clock(session)
            execution = session.scalar(
                select(QuestionExecution)
                .where(
                    QuestionExecution.run_id == run_id,
                    QuestionExecution.question_id == question_id,
                )
                .with_for_update()
            )
            if execution is None:
                execution = QuestionExecution(run_id=run_id, question_id=question_id)
                session.add(execution)
                session.flush()
            run = session.scalar(
                select(EvaluationRun)
                .where(
                    EvaluationRun.id == run_id,
                    EvaluationRun.status == RunStatus.RUNNING,
                    EvaluationRun.lease_owner == lease_owner,
                    EvaluationRun.lease_token == lease_token,
                    EvaluationRun.lease_expires_at > now,
                    EvaluationRun.cancellation_requested.is_(False),
                )
                .with_for_update()
            )
            if run is None:
                raise GovernanceFenceLost("governance_lease_fence_lost")
            input_reservation = (
                run.input_token_reservation
                if run.input_token_reservation is not None
                else estimated_input_tokens
            )
            return ProviderAttemptContext(
                run_id=run_id,
                question_id=question_id,
                model_id=model_id,
                provider_scope=provider_scope,
                lease_token=lease_token,
                execution_generation=execution.execution_generation,
                next_provider_attempt=execution.next_provider_attempt,
                reserved_input_tokens=_nonnegative_int("reserved_input_tokens", input_reservation),
                reserved_output_tokens=_nonnegative_int(
                    "reserved_output_tokens", reserved_output_tokens
                ),
                reserved_cost_usd=(
                    _nonnegative_money("reserved_cost_usd", reserved_cost_usd)
                    if reserved_cost_usd is not None
                    else None
                ),
            )

    def reserve(
        self,
        context: ProviderAttemptContext,
        *,
        provider_attempt: int,
        lease_owner: str,
    ) -> ProviderAttemptPermit:
        """Atomically reserve all four scopes for one logical Provider attempt."""

        if provider_attempt < 1:
            raise ValueError("provider_attempt must be positive")
        input_tokens = _nonnegative_int("reserved_input_tokens", context.reserved_input_tokens)
        output_tokens = _nonnegative_int("reserved_output_tokens", context.reserved_output_tokens)
        reserved_cost = (
            _nonnegative_money("reserved_cost_usd", context.reserved_cost_usd)
            if context.reserved_cost_usd is not None
            else None
        )
        operation_key = (
            f"run:{context.run_id}:question:{context.question_id}:"
            f"generation:{context.execution_generation}:attempt:{provider_attempt}"
        )

        with self._session_factory() as session, session.begin():
            _begin_sqlite_write(session)
            now = self._clock(session)
            window_start = _minute_start(now)
            run_view = session.get(EvaluationRun, context.run_id)
            if run_view is None or run_view.governance_policy_id is None:
                raise GovernanceIntegrityError("governance_run_policy_missing")
            policy = session.get(GovernancePolicy, run_view.governance_policy_id)
            if policy is None:
                raise GovernanceIntegrityError("governance_policy_missing")
            _validate_policy_integrity(policy)
            scopes = self._lock_scopes(
                session,
                (
                    (GovernanceScopeType.GLOBAL, "global"),
                    (GovernanceScopeType.PROVIDER, context.provider_scope),
                    (GovernanceScopeType.MODEL, context.model_id),
                    (GovernanceScopeType.RUN, context.run_id),
                ),
                now=now,
            )
            self._reconcile_stale_locked(session, scopes, now=now)
            buckets = self._lock_buckets(
                session,
                scopes,
                policy_id=policy.id,
                window_start=window_start,
                now=now,
            )
            self._validate_bucket_materialization_locked(session, buckets.values())
            execution = session.scalar(
                select(QuestionExecution)
                .where(
                    QuestionExecution.run_id == context.run_id,
                    QuestionExecution.question_id == context.question_id,
                )
                .with_for_update()
            )
            run = session.scalar(
                select(EvaluationRun)
                .where(EvaluationRun.id == context.run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                run is None
                or run.status != RunStatus.RUNNING
                or run.cancellation_requested
                or run.lease_owner != lease_owner
                or run.lease_token != context.lease_token
                or run.lease_expires_at is None
                or _as_utc(run.lease_expires_at) <= now
            ):
                raise GovernanceFenceLost("governance_lease_fence_lost")
            if execution is None:
                raise GovernanceIntegrityError("governance_question_execution_missing")
            governance_snapshot = dict(run.model_parameters_snapshot or {}).get("governance")
            expected_policy_snapshot = _policy_snapshot(policy)
            if (
                run.governance_status == GovernanceRunStatus.LEGACY_UNMANAGED
                or not isinstance(governance_snapshot, Mapping)
                or any(
                    governance_snapshot.get(name) != value
                    for name, value in expected_policy_snapshot.items()
                )
                or governance_snapshot.get("provider_scope_key") != context.provider_scope
                or run.model_id != context.model_id
            ):
                raise GovernanceIntegrityError("governance_run_snapshot_mismatch")
            run_overrides = governance_snapshot.get("run_overrides")
            if not isinstance(run_overrides, Mapping) or dict(run_overrides) != (
                _run_override_snapshot(run)
            ):
                raise GovernanceIntegrityError("governance_run_override_snapshot_mismatch")
            if (
                execution.execution_generation != context.execution_generation
                or execution.next_provider_attempt != provider_attempt
            ):
                existing = session.scalar(
                    select(ProviderCallReservation).where(
                        ProviderCallReservation.operation_key == operation_key
                    )
                )
                if (
                    existing is not None
                    and existing.state == ProviderCallReservationState.RESERVED
                    and existing.lease_token == context.lease_token
                    and existing.lease_owner == lease_owner
                ):
                    return ProviderAttemptPermit(
                        reservation_id=existing.id,
                        provider_attempt=provider_attempt,
                    )
                raise GovernanceSettlementUnknown()

            max_provider_attempts = int(
                dict(dict(run.model_parameters_snapshot).get("execution", {}))
                .get("retry_policy", {})
                .get("max_attempts", 1)
            )
            if provider_attempt > max_provider_attempts:
                raise GovernanceExhausted("governance_provider_retry_exhausted")
            self._check_limits(
                policy,
                scopes,
                buckets,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reserved_cost=reserved_cost,
                run_overrides=run_overrides,
                now=now,
            )
            by_type = {scope.scope_type: scope for scope in scopes}
            reservation = ProviderCallReservation(
                operation_key=operation_key,
                policy_id=policy.id,
                question_execution_id=execution.id,
                run_id=context.run_id,
                question_id=context.question_id,
                model_id=context.model_id,
                global_scope_id=by_type[GovernanceScopeType.GLOBAL].id,
                provider_scope_id=by_type[GovernanceScopeType.PROVIDER].id,
                model_scope_id=by_type[GovernanceScopeType.MODEL].id,
                run_scope_id=by_type[GovernanceScopeType.RUN].id,
                execution_generation=context.execution_generation,
                provider_attempt=provider_attempt,
                lease_owner=lease_owner,
                lease_token=context.lease_token,
                state=ProviderCallReservationState.RESERVED,
                lease_expires_at=run.lease_expires_at,
                window_start=window_start,
                reserved_input_tokens=input_tokens,
                reserved_output_tokens=output_tokens,
                reserved_cost_usd=reserved_cost,
            )
            session.add(reservation)
            session.flush()
            for scope in scopes:
                scope.active_reservations += 1
                scope.reserved_requests += 1
                scope.reserved_input_tokens += input_tokens or 0
                scope.reserved_output_tokens += output_tokens or 0
                scope.reserved_cost_usd = _money(scope.reserved_cost_usd) + (
                    reserved_cost or Decimal(0)
                )
                bucket = buckets[scope.id]
                bucket.reserved_requests += 1
                bucket.reserved_input_tokens += input_tokens or 0
                bucket.reserved_output_tokens += output_tokens or 0
            execution.next_provider_attempt += 1
            append_audit_event(
                session,
                event_key=f"reservation:{reservation.id}:reserved",
                event_type="provider_attempt_reserved",
                occurred_at=now,
                payload={
                    "reserved_input_tokens": input_tokens,
                    "reserved_output_tokens": output_tokens,
                    "reserved_cost_usd": reserved_cost,
                },
                correlation_id=context.run_id,
                run_id=context.run_id,
                model_id=context.model_id,
                question_id=context.question_id,
                worker_id=lease_owner,
                reservation_id=reservation.id,
                attempt=run.attempt_count,
                provider_attempt=provider_attempt,
                lease_token=context.lease_token,
            )
            return ProviderAttemptPermit(
                reservation_id=reservation.id,
                provider_attempt=provider_attempt,
            )

    def mark_send_started(
        self,
        permit: ProviderAttemptPermit,
        *,
        lease_owner: str,
    ) -> None:
        """Commit the send-start fence before the adapter enters HTTP transport."""

        with self._session_factory() as session, session.begin():
            _begin_sqlite_write(session)
            now = self._clock(session)
            reservation_view = session.get(ProviderCallReservation, permit.reservation_id)
            if reservation_view is None:
                raise GovernanceIntegrityError("governance_reservation_missing")
            scopes = self._lock_reservation_scopes(session, reservation_view)
            old_buckets = self._lock_existing_buckets(session, reservation_view, scopes)
            send_window = _minute_start(now)
            if send_window < _as_utc(reservation_view.window_start):
                raise GovernanceIntegrityError("governance_database_clock_reversed")
            if send_window != _as_utc(reservation_view.window_start):
                new_buckets = self._lock_buckets(
                    session,
                    scopes,
                    policy_id=reservation_view.policy_id,
                    window_start=send_window,
                    now=now,
                )
            else:
                new_buckets = old_buckets
            self._validate_scope_materialization_locked(session, scopes)
            self._validate_bucket_materialization_locked(
                session,
                (*old_buckets.values(), *new_buckets.values()),
            )
            reservation = session.scalar(
                select(ProviderCallReservation)
                .where(ProviderCallReservation.id == permit.reservation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if reservation is None:
                raise GovernanceIntegrityError("governance_reservation_missing")
            if reservation.state not in {
                ProviderCallReservationState.RESERVED,
                ProviderCallReservationState.SEND_STARTED,
            }:
                raise GovernanceSettlementUnknown()
            policy = session.get(GovernancePolicy, reservation.policy_id)
            if policy is None:
                raise GovernanceIntegrityError("governance_policy_missing")
            _validate_policy_integrity(policy)
            run = session.scalar(
                select(EvaluationRun)
                .where(EvaluationRun.id == reservation.run_id)
                .with_for_update()
            )
            if (
                run is None
                or run.status != RunStatus.RUNNING
                or run.cancellation_requested
                or run.lease_owner != lease_owner
                or run.lease_token != reservation.lease_token
                or run.lease_expires_at is None
                or _as_utc(run.lease_expires_at) <= now
            ):
                raise GovernanceFenceLost("governance_lease_fence_lost")
            if reservation.state == ProviderCallReservationState.SEND_STARTED:
                return
            if new_buckets is not old_buckets:
                self._check_window_limits(
                    policy,
                    scopes,
                    new_buckets,
                    input_tokens=reservation.reserved_input_tokens,
                    output_tokens=reservation.reserved_output_tokens,
                    now=now,
                )
            for scope in scopes:
                scope.reserved_requests -= 1
                scope.consumed_requests += 1
                old_bucket = old_buckets[scope.id]
                new_bucket = new_buckets[scope.id]
                old_bucket.reserved_requests -= 1
                if new_bucket is not old_bucket:
                    input_tokens = reservation.reserved_input_tokens or 0
                    output_tokens = reservation.reserved_output_tokens or 0
                    old_bucket.reserved_input_tokens -= input_tokens
                    old_bucket.reserved_output_tokens -= output_tokens
                    new_bucket.reserved_input_tokens += input_tokens
                    new_bucket.reserved_output_tokens += output_tokens
                new_bucket.consumed_requests += 1
            reservation.state = ProviderCallReservationState.SEND_STARTED
            reservation.send_started_at = now
            reservation.window_start = send_window
            execution = session.get(QuestionExecution, reservation.question_execution_id)
            if execution is not None and execution.first_attempt_at is None:
                execution.first_attempt_at = now
            append_audit_event(
                session,
                event_key=f"reservation:{reservation.id}:send_started",
                event_type="provider_attempt_send_started",
                occurred_at=now,
                correlation_id=reservation.run_id,
                run_id=reservation.run_id,
                model_id=reservation.model_id,
                question_id=reservation.question_id,
                worker_id=lease_owner,
                reservation_id=reservation.id,
                provider_attempt=reservation.provider_attempt,
                lease_token=reservation.lease_token,
            )

    def finish(
        self,
        permit: ProviderAttemptPermit,
        *,
        disposition: ProviderAttemptDisposition,
        outcome: ProviderAttemptOutcome,
        input_tokens: int | None,
        output_tokens: int | None,
        actual_cost_usd: Decimal | None,
    ) -> None:
        """Release or settle one attempt; the first terminal CAS wins."""

        _nonnegative_int("input_tokens", input_tokens)
        _nonnegative_int("output_tokens", output_tokens)
        cost = (
            _nonnegative_money("actual_cost_usd", actual_cost_usd)
            if actual_cost_usd is not None
            else None
        )
        with self._session_factory() as session, session.begin():
            _begin_sqlite_write(session)
            now = self._clock(session)
            view = session.get(ProviderCallReservation, permit.reservation_id)
            if view is None:
                raise GovernanceIntegrityError("governance_reservation_missing")
            scopes = self._lock_reservation_scopes(session, view)
            buckets = self._lock_existing_buckets(session, view, scopes)
            self._validate_scope_materialization_locked(session, scopes)
            self._validate_bucket_materialization_locked(session, buckets.values())
            reservation = session.scalar(
                select(ProviderCallReservation)
                .where(ProviderCallReservation.id == permit.reservation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if reservation is None:
                raise GovernanceIntegrityError("governance_reservation_missing")
            if reservation.state not in _ACTIVE_STATES:
                return
            if disposition == ProviderAttemptDisposition.RELEASED_PRE_SEND:
                if reservation.state != ProviderCallReservationState.RESERVED:
                    raise GovernanceIntegrityError("governance_release_after_send")
                self._restart_question_execution_after_pre_send_locked(session, reservation)
                self._release_locked(session, reservation, scopes, buckets, now=now)
            else:
                if reservation.state != ProviderCallReservationState.SEND_STARTED:
                    raise GovernanceIntegrityError("governance_settle_before_send")
                conservative = disposition == ProviderAttemptDisposition.SETTLED_CONSERVATIVE
                self._settle_locked(
                    session,
                    reservation,
                    scopes,
                    buckets,
                    now=now,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    actual_cost=None if conservative else cost,
                    outcome=outcome.value,
                    reconciled=False,
                    force_conservative=conservative,
                )

    def reconcile_run_lease(self, *, run_id: str, lease_token: int) -> tuple[int, int]:
        """Reconcile active attempts owned by one no-longer-current Run lease."""

        with self._session_factory() as session, session.begin():
            _begin_sqlite_write(session)
            now = self._clock(session)
            views = tuple(
                session.scalars(
                    select(ProviderCallReservation).where(
                        ProviderCallReservation.run_id == run_id,
                        ProviderCallReservation.lease_token == lease_token,
                        ProviderCallReservation.state.in_(_ACTIVE_STATES),
                    )
                )
            )
            if not views:
                return 0, 0
            scope_ids = {
                scope_id
                for row in views
                for scope_id in (
                    row.global_scope_id,
                    row.provider_scope_id,
                    row.model_scope_id,
                    row.run_scope_id,
                )
                if scope_id is not None
            }
            scopes = self._lock_scopes_by_id(session, scope_ids)
            return self._reconcile_rows_locked(session, views, scopes, now=now)

    def renew_run_lease(
        self,
        *,
        run_id: str,
        lease_owner: str,
        lease_token: int,
        lease_for: timedelta,
    ) -> datetime | None:
        """Atomically renew a Run fence and every active attempt owned by it."""

        with self._session_factory() as session, session.begin():
            _begin_sqlite_write(session)
            now = self._clock(session)
            expires_at = now + lease_for
            views = tuple(
                session.scalars(
                    select(ProviderCallReservation).where(
                        ProviderCallReservation.run_id == run_id,
                        ProviderCallReservation.lease_owner == lease_owner,
                        ProviderCallReservation.lease_token == lease_token,
                        ProviderCallReservation.state.in_(_ACTIVE_STATES),
                    )
                )
            )
            scope_ids = {
                scope_id
                for row in views
                for scope_id in (
                    row.global_scope_id,
                    row.provider_scope_id,
                    row.model_scope_id,
                    row.run_scope_id,
                )
                if scope_id is not None
            }
            scopes = self._lock_scopes_by_id(session, scope_ids)
            self._validate_scope_materialization_locked(session, scopes)
            reservations = (
                list(
                    session.scalars(
                        select(ProviderCallReservation)
                        .where(ProviderCallReservation.id.in_([row.id for row in views]))
                        .order_by(ProviderCallReservation.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                if views
                else []
            )
            run = session.scalar(
                select(EvaluationRun)
                .where(
                    EvaluationRun.id == run_id,
                    EvaluationRun.status == RunStatus.RUNNING,
                    EvaluationRun.cancellation_requested.is_(False),
                    EvaluationRun.lease_owner == lease_owner,
                    EvaluationRun.lease_token == lease_token,
                    EvaluationRun.lease_expires_at > now,
                )
                .with_for_update()
            )
            if run is None:
                return None
            run.heartbeat_at = now
            run.lease_expires_at = expires_at
            for reservation in reservations:
                if reservation.state in _ACTIVE_STATES:
                    reservation.lease_expires_at = expires_at
            return expires_at

    def _lock_scopes(
        self,
        session: Session,
        identities: Iterable[tuple[GovernanceScopeType, str]],
        *,
        now: datetime,
    ) -> list[GovernanceScope]:
        ordered = sorted(set(identities), key=lambda item: (_SCOPE_ORDER[item[0]], item[1]))
        for scope_type, scope_key in ordered:
            _insert_do_nothing(
                session,
                GovernanceScope.__table__,
                {
                    "id": str(uuid4()),
                    "scope_type": scope_type.value,
                    "scope_key": scope_key,
                    "active_reservations": 0,
                    "reserved_requests": 0,
                    "reserved_input_tokens": 0,
                    "reserved_output_tokens": 0,
                    "reserved_cost_usd": Decimal(0),
                    "consumed_requests": 0,
                    "consumed_input_tokens": 0,
                    "consumed_output_tokens": 0,
                    "consumed_cost_usd": Decimal(0),
                    "overdrawn": False,
                    "created_at": now,
                    "updated_at": now,
                },
                conflict_columns=("scope_type", "scope_key"),
            )
        rank = case(
            *(
                (GovernanceScope.scope_type == scope_type, position)
                for scope_type, position in _SCOPE_ORDER.items()
            ),
            else_=99,
        )
        clauses = [
            (GovernanceScope.scope_type == scope_type) & (GovernanceScope.scope_key == scope_key)
            for scope_type, scope_key in ordered
        ]
        return list(
            session.scalars(
                select(GovernanceScope)
                .where(or_(*clauses))
                .order_by(rank, GovernanceScope.scope_key)
                .with_for_update()
            )
        )

    def _lock_scopes_by_id(
        self, session: Session, scope_ids: Iterable[str]
    ) -> list[GovernanceScope]:
        ids = tuple(sorted(set(scope_ids)))
        if not ids:
            return []
        rank = case(
            *(
                (GovernanceScope.scope_type == scope_type, position)
                for scope_type, position in _SCOPE_ORDER.items()
            ),
            else_=99,
        )
        return list(
            session.scalars(
                select(GovernanceScope)
                .where(GovernanceScope.id.in_(ids))
                .order_by(rank, GovernanceScope.scope_key)
                .with_for_update()
            )
        )

    def _lock_buckets(
        self,
        session: Session,
        scopes: Sequence[GovernanceScope],
        *,
        policy_id: str,
        window_start: datetime,
        now: datetime,
    ) -> dict[str, GovernanceMinuteBucket]:
        for scope in scopes:
            _insert_do_nothing(
                session,
                GovernanceMinuteBucket.__table__,
                {
                    "id": str(uuid4()),
                    "scope_id": scope.id,
                    "policy_id": policy_id,
                    "window_start": window_start,
                    "reserved_requests": 0,
                    "reserved_input_tokens": 0,
                    "reserved_output_tokens": 0,
                    "consumed_requests": 0,
                    "consumed_input_tokens": 0,
                    "consumed_output_tokens": 0,
                    "created_at": now,
                    "updated_at": now,
                },
                conflict_columns=("scope_id", "policy_id", "window_start"),
            )
        rows = list(
            session.scalars(
                select(GovernanceMinuteBucket)
                .where(
                    GovernanceMinuteBucket.scope_id.in_([scope.id for scope in scopes]),
                    GovernanceMinuteBucket.policy_id == policy_id,
                    GovernanceMinuteBucket.window_start == window_start,
                )
                .order_by(GovernanceMinuteBucket.scope_id)
                .with_for_update()
            )
        )
        return {row.scope_id: row for row in rows}

    def _lock_existing_buckets(
        self,
        session: Session,
        reservation: ProviderCallReservation,
        scopes: Sequence[GovernanceScope],
    ) -> dict[str, GovernanceMinuteBucket]:
        rows = list(
            session.scalars(
                select(GovernanceMinuteBucket)
                .where(
                    GovernanceMinuteBucket.scope_id.in_([scope.id for scope in scopes]),
                    GovernanceMinuteBucket.policy_id == reservation.policy_id,
                    GovernanceMinuteBucket.window_start == reservation.window_start,
                )
                .order_by(GovernanceMinuteBucket.scope_id)
                .with_for_update()
            )
        )
        if len(rows) != len(scopes):
            raise GovernanceIntegrityError("governance_minute_bucket_missing")
        return {row.scope_id: row for row in rows}

    def _lock_reservation_scopes(
        self, session: Session, reservation: ProviderCallReservation
    ) -> list[GovernanceScope]:
        return self._lock_scopes_by_id(
            session,
            (
                reservation.global_scope_id,
                reservation.provider_scope_id,
                reservation.model_scope_id,
                *([reservation.run_scope_id] if reservation.run_scope_id else []),
            ),
        )

    @staticmethod
    def _scope_fact_source():
        """Expand every immutable ledger row into its three or four scope facts."""

        reservation = ProviderCallReservation
        columns = (
            reservation.state.label("state"),
            reservation.policy_id.label("policy_id"),
            reservation.window_start.label("window_start"),
            reservation.reserved_input_tokens.label("reserved_input_tokens"),
            reservation.reserved_output_tokens.label("reserved_output_tokens"),
            reservation.reserved_cost_usd.label("reserved_cost_usd"),
            reservation.actual_input_tokens.label("actual_input_tokens"),
            reservation.actual_output_tokens.label("actual_output_tokens"),
            reservation.actual_cost_usd.label("actual_cost_usd"),
        )
        scope_columns = (
            reservation.global_scope_id,
            reservation.provider_scope_id,
            reservation.model_scope_id,
            reservation.run_scope_id,
        )
        return union_all(
            *(
                select(scope_column.label("scope_id"), *columns).where(scope_column.is_not(None))
                for scope_column in scope_columns
            )
        ).subquery()

    @staticmethod
    def _scope_fact_aggregates(facts: Any) -> tuple[Any, ...]:
        overdrawn = and_(
            facts.c.state.in_(_SETTLED_STATES),
            or_(
                and_(
                    facts.c.reserved_input_tokens.is_not(None),
                    facts.c.actual_input_tokens.is_not(None),
                    facts.c.actual_input_tokens > facts.c.reserved_input_tokens,
                ),
                and_(
                    facts.c.reserved_output_tokens.is_not(None),
                    facts.c.actual_output_tokens.is_not(None),
                    facts.c.actual_output_tokens > facts.c.reserved_output_tokens,
                ),
                and_(
                    facts.c.reserved_cost_usd.is_not(None),
                    facts.c.actual_cost_usd.is_not(None),
                    facts.c.actual_cost_usd > facts.c.reserved_cost_usd,
                ),
            ),
        )
        return (
            func.coalesce(func.sum(case((facts.c.state.in_(_ACTIVE_STATES), 1), else_=0)), 0).label(
                "active_reservations"
            ),
            func.coalesce(
                func.sum(
                    case(
                        (facts.c.state == ProviderCallReservationState.RESERVED, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("reserved_requests"),
            func.coalesce(
                func.sum(case((facts.c.state.in_(_CONSUMED_REQUEST_STATES), 1), else_=0)),
                0,
            ).label("consumed_requests"),
            func.coalesce(
                func.sum(
                    case(
                        (facts.c.state.in_(_ACTIVE_STATES), facts.c.reserved_input_tokens),
                        else_=0,
                    )
                ),
                0,
            ).label("reserved_input_tokens"),
            func.coalesce(
                func.sum(
                    case(
                        (facts.c.state.in_(_ACTIVE_STATES), facts.c.reserved_output_tokens),
                        else_=0,
                    )
                ),
                0,
            ).label("reserved_output_tokens"),
            func.coalesce(
                func.sum(
                    case(
                        (facts.c.state.in_(_ACTIVE_STATES), facts.c.reserved_cost_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("reserved_cost_usd"),
            func.coalesce(
                func.sum(
                    case(
                        (facts.c.state.in_(_SETTLED_STATES), facts.c.actual_input_tokens),
                        else_=0,
                    )
                ),
                0,
            ).label("consumed_input_tokens"),
            func.coalesce(
                func.sum(
                    case(
                        (facts.c.state.in_(_SETTLED_STATES), facts.c.actual_output_tokens),
                        else_=0,
                    )
                ),
                0,
            ).label("consumed_output_tokens"),
            func.coalesce(
                func.sum(
                    case(
                        (facts.c.state.in_(_SETTLED_STATES), facts.c.actual_cost_usd),
                        else_=0,
                    )
                ),
                0,
            ).label("consumed_cost_usd"),
            func.coalesce(func.max(case((overdrawn, 1), else_=0)), 0).label("overdrawn"),
        )

    def _validate_scope_materialization_locked(
        self,
        session: Session,
        scopes: Sequence[GovernanceScope],
    ) -> None:
        """Fail closed when a locked materialized scope differs from the ledger."""

        if not scopes:
            return
        facts = self._scope_fact_source()
        rows = session.execute(
            select(facts.c.scope_id, *self._scope_fact_aggregates(facts))
            .where(facts.c.scope_id.in_([scope.id for scope in scopes]))
            .group_by(facts.c.scope_id)
        ).all()
        by_scope = {row.scope_id: row for row in rows}
        for scope in scopes:
            row = by_scope.get(scope.id)
            derived = (
                int(row.active_reservations) if row else 0,
                int(row.reserved_requests) if row else 0,
                int(row.reserved_input_tokens) if row else 0,
                int(row.reserved_output_tokens) if row else 0,
                _money(row.reserved_cost_usd) if row else Decimal(0),
                int(row.consumed_requests) if row else 0,
                int(row.consumed_input_tokens) if row else 0,
                int(row.consumed_output_tokens) if row else 0,
                _money(row.consumed_cost_usd) if row else Decimal(0),
                bool(row.overdrawn) if row else False,
            )
            materialized = (
                scope.active_reservations,
                scope.reserved_requests,
                scope.reserved_input_tokens,
                scope.reserved_output_tokens,
                _money(scope.reserved_cost_usd),
                scope.consumed_requests,
                scope.consumed_input_tokens,
                scope.consumed_output_tokens,
                _money(scope.consumed_cost_usd),
                scope.overdrawn,
            )
            if materialized != derived:
                raise GovernanceIntegrityError("governance_scope_counter_drift")

    def _validate_bucket_materialization_locked(
        self,
        session: Session,
        buckets: Iterable[GovernanceMinuteBucket],
    ) -> None:
        """Fail closed when locked fixed-window counters differ from the ledger."""

        unique = {
            (bucket.scope_id, bucket.policy_id, _as_utc(bucket.window_start)): bucket
            for bucket in buckets
        }
        if not unique:
            return
        facts = self._scope_fact_source()
        clauses = [
            and_(
                facts.c.scope_id == scope_id,
                facts.c.policy_id == policy_id,
                facts.c.window_start == window_start,
            )
            for scope_id, policy_id, window_start in unique
        ]
        aggregates = self._scope_fact_aggregates(facts)
        rows = session.execute(
            select(
                facts.c.scope_id,
                facts.c.policy_id,
                facts.c.window_start,
                *aggregates,
            )
            .where(or_(*clauses))
            .group_by(facts.c.scope_id, facts.c.policy_id, facts.c.window_start)
        ).all()
        derived = {
            (row.scope_id, row.policy_id, _as_utc(row.window_start)): (
                int(row.reserved_requests),
                int(row.reserved_input_tokens),
                int(row.reserved_output_tokens),
                int(row.consumed_requests),
                int(row.consumed_input_tokens),
                int(row.consumed_output_tokens),
            )
            for row in rows
        }
        for key, bucket in unique.items():
            materialized = (
                bucket.reserved_requests,
                bucket.reserved_input_tokens,
                bucket.reserved_output_tokens,
                bucket.consumed_requests,
                bucket.consumed_input_tokens,
                bucket.consumed_output_tokens,
            )
            if materialized != derived.get(key, (0, 0, 0, 0, 0, 0)):
                raise GovernanceIntegrityError("governance_minute_bucket_counter_drift")

    def _check_limits(
        self,
        policy: GovernancePolicy,
        scopes: Sequence[GovernanceScope],
        buckets: dict[str, GovernanceMinuteBucket],
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        reserved_cost: Decimal | None,
        run_overrides: Mapping[str, Any],
        now: datetime,
    ) -> None:
        run_input_bound = _nonnegative_int(
            "input_token_reservation",
            run_overrides.get("input_token_reservation"),
        )
        run_request_budget = _nonnegative_int(
            "lifetime_request_budget",
            run_overrides.get("lifetime_request_budget"),
        )
        run_token_budget = _nonnegative_int(
            "lifetime_token_budget",
            run_overrides.get("lifetime_token_budget"),
        )
        run_cost_value = run_overrides.get("lifetime_cost_budget_usd")
        run_cost_budget = (
            _nonnegative_money("lifetime_cost_budget_usd", run_cost_value)
            if run_cost_value is not None
            else None
        )
        hard_token_enabled = any(
            getattr(policy, f"{scope.scope_type.value}_tokens_per_minute") is not None
            for scope in scopes
        ) or any(
            value is not None
            for value in (
                policy.global_lifetime_token_budget,
                policy.run_lifetime_token_budget,
                run_token_budget,
            )
        )
        hard_cost_enabled = any(
            value is not None
            for value in (
                policy.global_lifetime_cost_budget_usd,
                policy.run_lifetime_cost_budget_usd,
                run_cost_budget,
            )
        )
        finite_token_bound_required = hard_token_enabled or hard_cost_enabled
        if finite_token_bound_required and (run_input_bound is None or input_tokens is None):
            raise GovernanceExhausted("governance_input_bound_unknown")
        if finite_token_bound_required and output_tokens is None:
            raise GovernanceExhausted("governance_unbounded_output")
        if hard_cost_enabled and reserved_cost is None:
            raise GovernanceExhausted("governance_pricing_unknown")

        new_tokens = (input_tokens or 0) + (output_tokens or 0)
        window_end = _minute_start(now) + timedelta(minutes=1)
        for scope in scopes:
            prefix = scope.scope_type.value
            if scope.overdrawn:
                raise GovernanceExhausted(f"governance_{prefix}_overdrawn")
            concurrency_limit = getattr(policy, f"{prefix}_concurrency_limit")
            if concurrency_limit is not None and scope.active_reservations + 1 > concurrency_limit:
                raise GovernanceDeferred(
                    f"governance_{prefix}_concurrency",
                    not_before=now + timedelta(seconds=1),
                )
            bucket = buckets[scope.id]
            request_limit = getattr(policy, f"{prefix}_requests_per_minute")
            if (
                request_limit is not None
                and bucket.reserved_requests + bucket.consumed_requests + 1 > request_limit
            ):
                raise GovernanceDeferred(
                    f"governance_{prefix}_rpm",
                    not_before=window_end,
                )
            token_limit = getattr(policy, f"{prefix}_tokens_per_minute")
            bucket_tokens = (
                bucket.reserved_input_tokens
                + bucket.reserved_output_tokens
                + bucket.consumed_input_tokens
                + bucket.consumed_output_tokens
            )
            if token_limit is not None and bucket_tokens + new_tokens > token_limit:
                raise GovernanceDeferred(
                    f"governance_{prefix}_tpm",
                    not_before=window_end,
                )

            if scope.scope_type not in {
                GovernanceScopeType.GLOBAL,
                GovernanceScopeType.RUN,
            }:
                continue
            request_budget = getattr(policy, f"{prefix}_lifetime_request_budget")
            token_budget = getattr(policy, f"{prefix}_lifetime_token_budget")
            cost_budget = getattr(policy, f"{prefix}_lifetime_cost_budget_usd")
            if scope.scope_type == GovernanceScopeType.RUN:
                request_budget = self._stricter(request_budget, run_request_budget)
                token_budget = self._stricter(token_budget, run_token_budget)
                cost_budget = self._stricter(cost_budget, run_cost_budget)
            if (
                request_budget is not None
                and scope.reserved_requests + scope.consumed_requests + 1 > request_budget
            ):
                raise GovernanceExhausted(f"governance_{prefix}_request_budget_exhausted")
            lifetime_tokens = (
                scope.reserved_input_tokens
                + scope.reserved_output_tokens
                + scope.consumed_input_tokens
                + scope.consumed_output_tokens
            )
            if token_budget is not None and lifetime_tokens + new_tokens > token_budget:
                raise GovernanceExhausted(f"governance_{prefix}_token_budget_exhausted")
            lifetime_cost = _money(scope.reserved_cost_usd) + _money(scope.consumed_cost_usd)
            if cost_budget is not None and lifetime_cost + (reserved_cost or Decimal(0)) > _money(
                cost_budget
            ):
                raise GovernanceExhausted(f"governance_{prefix}_cost_budget_exhausted")

    @staticmethod
    def _check_window_limits(
        policy: GovernancePolicy,
        scopes: Sequence[GovernanceScope],
        buckets: dict[str, GovernanceMinuteBucket],
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        now: datetime,
    ) -> None:
        """Revalidate fixed-window limits when send-start crosses a UTC minute."""

        new_tokens = (input_tokens or 0) + (output_tokens or 0)
        window_end = _minute_start(now) + timedelta(minutes=1)
        for scope in scopes:
            prefix = scope.scope_type.value
            bucket = buckets[scope.id]
            request_limit = getattr(policy, f"{prefix}_requests_per_minute")
            if (
                request_limit is not None
                and bucket.reserved_requests + bucket.consumed_requests + 1 > request_limit
            ):
                raise GovernanceDeferred(
                    f"governance_{prefix}_rpm",
                    not_before=window_end,
                )
            token_limit = getattr(policy, f"{prefix}_tokens_per_minute")
            bucket_tokens = (
                bucket.reserved_input_tokens
                + bucket.reserved_output_tokens
                + bucket.consumed_input_tokens
                + bucket.consumed_output_tokens
            )
            if token_limit is not None and bucket_tokens + new_tokens > token_limit:
                raise GovernanceDeferred(
                    f"governance_{prefix}_tpm",
                    not_before=window_end,
                )

    @staticmethod
    def _stricter(first: Any, second: Any) -> Any:
        values = [value for value in (first, second) if value is not None]
        return min(values) if values else None

    @staticmethod
    def _restart_question_execution_after_pre_send_locked(
        session: Session,
        reservation: ProviderCallReservation,
    ) -> None:
        """Start a fresh local generation when no Provider request was sent.

        The terminal ledger row cannot be reused because its logical key is
        immutable. Advancing the local generation preserves that history while
        ensuring a confirmed pre-send release consumes no HTTP retry ordinal.
        Lease-loss reconciliation already advances generations at takeover and
        therefore deliberately does not call this helper.
        """

        if reservation.question_execution_id is None:
            return
        execution = session.scalar(
            select(QuestionExecution)
            .where(QuestionExecution.id == reservation.question_execution_id)
            .with_for_update()
        )
        if (
            execution is None
            or execution.execution_generation != reservation.execution_generation
            or execution.next_provider_attempt != reservation.provider_attempt + 1
        ):
            raise GovernanceIntegrityError("governance_question_retry_cursor_mismatch")
        execution.execution_generation += 1
        # Earlier ordinals in this question may already have reached HTTP and
        # must remain consumed. Only the current, provably unsent ordinal is
        # retried under the new ledger generation.
        execution.next_provider_attempt = reservation.provider_attempt

    def _release_locked(
        self,
        session: Session,
        reservation: ProviderCallReservation,
        scopes: Sequence[GovernanceScope],
        buckets: dict[str, GovernanceMinuteBucket],
        *,
        now: datetime,
        reconciled: bool = False,
    ) -> None:
        input_tokens = reservation.reserved_input_tokens or 0
        output_tokens = reservation.reserved_output_tokens or 0
        cost = _money(reservation.reserved_cost_usd)
        for scope in scopes:
            scope.active_reservations -= 1
            scope.reserved_requests -= 1
            scope.reserved_input_tokens -= input_tokens
            scope.reserved_output_tokens -= output_tokens
            scope.reserved_cost_usd = _money(scope.reserved_cost_usd) - cost
            bucket = buckets[scope.id]
            bucket.reserved_requests -= 1
            bucket.reserved_input_tokens -= input_tokens
            bucket.reserved_output_tokens -= output_tokens
        reservation.state = ProviderCallReservationState.RELEASED_PRE_SEND
        reservation.outcome_code = (
            "lease_reconciled_pre_send" if reconciled else "released_pre_send"
        )
        reservation.settled_at = now
        self._append_settlement_audit(
            session,
            reservation,
            disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND.value,
            outcome=reservation.outcome_code,
            input_tokens=None,
            output_tokens=None,
            cost=None,
            reconciled=reconciled,
            now=now,
        )

    def _settle_locked(
        self,
        session: Session,
        reservation: ProviderCallReservation,
        scopes: Sequence[GovernanceScope],
        buckets: dict[str, GovernanceMinuteBucket],
        *,
        now: datetime,
        input_tokens: int | None,
        output_tokens: int | None,
        actual_cost: Decimal | None,
        outcome: str,
        reconciled: bool,
        force_conservative: bool = False,
    ) -> None:
        reserved_input = reservation.reserved_input_tokens or 0
        reserved_output = reservation.reserved_output_tokens or 0
        reserved_cost = _money(reservation.reserved_cost_usd)
        settled_input = reservation.reserved_input_tokens if input_tokens is None else input_tokens
        settled_output = (
            reservation.reserved_output_tokens if output_tokens is None else output_tokens
        )
        settled_cost = reservation.reserved_cost_usd if actual_cost is None else _money(actual_cost)
        conservative = (
            force_conservative or reconciled or input_tokens is None or output_tokens is None
        )
        for scope in scopes:
            scope.active_reservations -= 1
            scope.reserved_input_tokens -= reserved_input
            scope.reserved_output_tokens -= reserved_output
            scope.reserved_cost_usd = _money(scope.reserved_cost_usd) - reserved_cost
            scope.consumed_input_tokens += settled_input or 0
            scope.consumed_output_tokens += settled_output or 0
            scope.consumed_cost_usd = _money(scope.consumed_cost_usd) + _money(settled_cost)
            if (
                (
                    reservation.reserved_input_tokens is not None
                    and settled_input is not None
                    and settled_input > reserved_input
                )
                or (
                    reservation.reserved_output_tokens is not None
                    and settled_output is not None
                    and settled_output > reserved_output
                )
                or (
                    reservation.reserved_cost_usd is not None
                    and settled_cost is not None
                    and _money(settled_cost) > reserved_cost
                )
            ):
                scope.overdrawn = True
            bucket = buckets[scope.id]
            bucket.reserved_input_tokens -= reserved_input
            bucket.reserved_output_tokens -= reserved_output
            bucket.consumed_input_tokens += settled_input or 0
            bucket.consumed_output_tokens += settled_output or 0
        reservation.state = (
            ProviderCallReservationState.SETTLED_CONSERVATIVE
            if conservative
            else ProviderCallReservationState.SETTLED_ACTUAL
        )
        reservation.actual_input_tokens = settled_input
        reservation.actual_output_tokens = settled_output
        reservation.actual_cost_usd = settled_cost
        reservation.outcome_code = outcome
        reservation.settled_at = now
        self._append_settlement_audit(
            session,
            reservation,
            disposition=reservation.state.value,
            outcome=outcome,
            input_tokens=settled_input,
            output_tokens=settled_output,
            cost=settled_cost,
            reconciled=reconciled,
            now=now,
        )

    @staticmethod
    def _append_settlement_audit(
        session: Session,
        reservation: ProviderCallReservation,
        *,
        disposition: str,
        outcome: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cost: Decimal | None,
        reconciled: bool,
        now: datetime,
    ) -> None:
        append_audit_event(
            session,
            event_key=f"reservation:{reservation.id}:settled",
            event_type="provider_attempt_settled",
            occurred_at=now,
            payload={
                "disposition": disposition,
                "outcome": outcome,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
                "reconciled": reconciled,
            },
            correlation_id=reservation.run_id,
            run_id=reservation.run_id,
            model_id=reservation.model_id,
            question_id=reservation.question_id,
            worker_id=reservation.lease_owner,
            reservation_id=reservation.id,
            provider_attempt=reservation.provider_attempt,
            lease_token=reservation.lease_token,
        )

    def _reconcile_stale_locked(
        self,
        session: Session,
        scopes: Sequence[GovernanceScope],
        *,
        now: datetime,
    ) -> tuple[int, int]:
        scope_ids = [scope.id for scope in scopes]
        views = tuple(
            session.scalars(
                select(ProviderCallReservation).where(
                    ProviderCallReservation.state.in_(_ACTIVE_STATES),
                    or_(
                        ProviderCallReservation.global_scope_id.in_(scope_ids),
                        ProviderCallReservation.provider_scope_id.in_(scope_ids),
                        ProviderCallReservation.model_scope_id.in_(scope_ids),
                        ProviderCallReservation.run_scope_id.in_(scope_ids),
                    ),
                )
            )
        )
        all_scope_ids = set(scope_ids) | {
            scope_id
            for row in views
            for scope_id in (
                row.global_scope_id,
                row.provider_scope_id,
                row.model_scope_id,
                row.run_scope_id,
            )
            if scope_id is not None
        }
        locked_scopes = self._lock_scopes_by_id(session, all_scope_ids)
        return self._reconcile_rows_locked(session, views, locked_scopes, now=now)

    def _reconcile_rows_locked(
        self,
        session: Session,
        views: Sequence[ProviderCallReservation],
        scopes: Sequence[GovernanceScope],
        *,
        now: datetime,
    ) -> tuple[int, int]:
        released = 0
        conservative = 0
        per_lease: dict[tuple[str, int, str, str | None], list[int]] = {}
        scopes_by_id = {scope.id: scope for scope in scopes}
        self._validate_scope_materialization_locked(session, scopes)
        validated_buckets: set[tuple[str, str, datetime]] = set()
        for view in sorted(views, key=lambda item: item.id):
            if view.state not in _ACTIVE_STATES:
                continue
            row_scopes = [
                scopes_by_id[scope_id]
                for scope_id in (
                    view.global_scope_id,
                    view.provider_scope_id,
                    view.model_scope_id,
                    view.run_scope_id,
                )
                if scope_id is not None and scope_id in scopes_by_id
            ]
            if len(row_scopes) not in {3, 4}:
                raise GovernanceIntegrityError("governance_scope_missing")
            buckets = self._lock_existing_buckets(session, view, row_scopes)
            bucket_keys = {
                (bucket.scope_id, bucket.policy_id, _as_utc(bucket.window_start))
                for bucket in buckets.values()
            }
            if not bucket_keys.issubset(validated_buckets):
                self._validate_bucket_materialization_locked(session, buckets.values())
                validated_buckets.update(bucket_keys)
            reservation = session.scalar(
                select(ProviderCallReservation)
                .where(ProviderCallReservation.id == view.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if reservation is None or reservation.state not in _ACTIVE_STATES:
                continue
            run = (
                session.scalar(
                    select(EvaluationRun)
                    .where(EvaluationRun.id == reservation.run_id)
                    .with_for_update()
                )
                if reservation.run_id
                else None
            )
            still_owned = bool(
                run is not None
                and run.status == RunStatus.RUNNING
                and not run.cancellation_requested
                and run.lease_owner == reservation.lease_owner
                and run.lease_token == reservation.lease_token
                and run.lease_expires_at is not None
                and _as_utc(run.lease_expires_at) > now
            )
            if still_owned:
                continue
            if reservation.state == ProviderCallReservationState.RESERVED:
                self._release_locked(
                    session,
                    reservation,
                    row_scopes,
                    buckets,
                    now=now,
                    reconciled=True,
                )
                released += 1
                transition_index = 0
            else:
                self._settle_locked(
                    session,
                    reservation,
                    row_scopes,
                    buckets,
                    now=now,
                    input_tokens=None,
                    output_tokens=None,
                    actual_cost=None,
                    outcome="lease_reconciled_unknown",
                    reconciled=True,
                )
                conservative += 1
                transition_index = 1
            if reservation.run_id is not None and reservation.lease_token is not None:
                key = (
                    reservation.run_id,
                    int(reservation.lease_token),
                    reservation.model_id,
                    reservation.lease_owner,
                )
                counts = per_lease.setdefault(key, [0, 0])
                counts[transition_index] += 1
        if released or conservative:
            session.flush()
        for (run_id, lease_token, model_id, lease_owner), counts in per_lease.items():
            append_audit_event(
                session,
                event_key=f"run:{run_id}:lease:{lease_token}:reconciled",
                event_type="run_lease_reconciled",
                occurred_at=now,
                payload={
                    "released_reservations": counts[0],
                    "conservative_settlements": counts[1],
                },
                correlation_id=run_id,
                run_id=run_id,
                model_id=model_id,
                worker_id=lease_owner,
                lease_token=lease_token,
            )
        return released, conservative


class DatabaseProviderAttemptController(ProviderAttemptController):
    """Async Adapter hook backed by the synchronous governance repository."""

    def __init__(
        self,
        repository: GovernanceRepository,
        *,
        lease_owner: str,
        input_price_per_million: Decimal | None,
        output_price_per_million: Decimal | None,
    ) -> None:
        self._repository = repository
        self._lease_owner = lease_owner
        self._input_price = input_price_per_million
        self._output_price = output_price_per_million

    async def reserve(
        self,
        context: ProviderAttemptContext,
        *,
        provider_attempt: int,
    ) -> ProviderAttemptPermit:
        try:
            return await asyncio.to_thread(
                self._repository.reserve,
                context,
                provider_attempt=provider_attempt,
                lease_owner=self._lease_owner,
            )
        except GovernanceControlSignal:
            raise
        except SQLAlchemyError:
            raise GovernanceSettlementUnknown() from None

    async def mark_send_started(self, permit: ProviderAttemptPermit) -> None:
        try:
            await asyncio.to_thread(
                self._repository.mark_send_started,
                permit,
                lease_owner=self._lease_owner,
            )
        except GovernanceControlSignal:
            raise
        except SQLAlchemyError:
            raise GovernanceSettlementUnknown() from None

    async def finish(
        self,
        permit: ProviderAttemptPermit,
        *,
        disposition: ProviderAttemptDisposition,
        outcome: ProviderAttemptOutcome,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        actual_cost = self._actual_cost(input_tokens, output_tokens)
        try:
            await asyncio.to_thread(
                self._repository.finish,
                permit,
                disposition=disposition,
                outcome=outcome,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                actual_cost_usd=actual_cost,
            )
        except GovernanceControlSignal:
            raise
        except SQLAlchemyError:
            raise GovernanceSettlementUnknown() from None

    def _actual_cost(self, input_tokens: int | None, output_tokens: int | None) -> Decimal | None:
        if (
            input_tokens is None
            or output_tokens is None
            or self._input_price is None
            or self._output_price is None
        ):
            return None
        value = (
            Decimal(input_tokens) * self._input_price + Decimal(output_tokens) * self._output_price
        ) / Decimal(1_000_000)
        return _money(value)
