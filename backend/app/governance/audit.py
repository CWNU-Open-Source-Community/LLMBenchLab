"""Typed, secret-minimized application audit events.

The table is append-only by application contract, not a WORM or cryptographic
log.  Callers cannot pass arbitrary JSON: every event type has an explicit
payload field allowlist and every string value is constrained to a short safe
token.  Prompt, response, URL, exception text, credentials, encrypted envelopes,
and raw Provider usage have no representable field here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import AuditEvent, AuditRetentionClass
from app.security.provider_metadata import normalize_provider_metadata


class AuditIntegrityError(RuntimeError):
    """Raised when an event key is replayed with a different durable fact."""


_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:@/-]{1,128}")
_SAFE_IDENTITY_TOKEN = re.compile(r"[A-Za-z0-9_.:@/-]{1,255}")
_SAFE_HASH = re.compile(r"[a-f0-9]{64}")
_SAFE_DECIMAL = re.compile(r"0|[1-9][0-9]{0,19}(?:\.[0-9]{1,8})?|0\.[0-9]{1,8}")
_INT32_MAX = 2**31 - 1
_INT64_MAX = 2**63 - 1


@dataclass(frozen=True)
class AuditEventReadFacts:
    """Validated, exact storage facts for one retained audit event.

    Unlike the business replay comparison in :func:`_event_matches`, this
    projection includes the primary key and both timestamps.  Retention
    archive reconciliation can therefore compare every persisted fact without
    weakening append-event idempotency semantics.
    """

    id: str
    event_key: str
    event_type: str
    payload_hash: str
    payload: dict[str, Any]
    retention_class: AuditRetentionClass
    occurred_at: datetime
    expires_at: datetime
    correlation_id: str | None
    run_id: str | None
    model_id: str | None
    question_id: str | None
    worker_id: str | None
    reservation_id: str | None
    attempt: int | None
    provider_attempt: int | None
    lease_token: int | None
    duration_ms: float | None

    def as_insert_values(self) -> dict[str, Any]:
        """Return a fresh mapping suitable for an exact ORM insert."""

        return {
            "id": self.id,
            "event_key": self.event_key,
            "event_type": self.event_type,
            "payload_hash": self.payload_hash,
            "payload": dict(self.payload),
            "retention_class": self.retention_class,
            "occurred_at": self.occurred_at,
            "expires_at": self.expires_at,
            "correlation_id": self.correlation_id,
            "run_id": self.run_id,
            "model_id": self.model_id,
            "question_id": self.question_id,
            "worker_id": self.worker_id,
            "reservation_id": self.reservation_id,
            "attempt": self.attempt,
            "provider_attempt": self.provider_attempt,
            "lease_token": self.lease_token,
            "duration_ms": self.duration_ms,
        }


# Frozen archive-v1 payload boundaries.  The live contract starts as a copy of
# these values so future event evolution cannot silently reinterpret a
# long-lived archive.  An empty set means the transition columns themselves are
# sufficient evidence and no JSON payload is accepted.
_ARCHIVE_V1_EVENT_PAYLOAD_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "governance_policy_bootstrapped": frozenset({"policy_version", "policy_hash"}),
        "governance_policy_applied": frozenset({"policy_version", "policy_hash"}),
        "run_admitted": frozenset({"policy_version", "backlog_count", "question_quantum"}),
        "run_claimed": frozenset({"dispatch_count"}),
        "run_cancel_requested": frozenset(),
        "run_deferred": frozenset({"reason", "not_before"}),
        "run_yielded": frozenset({"responses_added"}),
        "run_terminal": frozenset({"status", "reason"}),
        "run_retry_scheduled": frozenset({"failed_attempt_count", "reason"}),
        "run_dead_lettered": frozenset({"failed_attempt_count", "reason"}),
        "run_lease_reconciled": frozenset({"released_reservations", "conservative_settlements"}),
        "provider_attempt_reserved": frozenset(
            {
                "reserved_input_tokens",
                "reserved_output_tokens",
                "reserved_cost_usd",
            }
        ),
        "provider_attempt_send_started": frozenset(),
        "provider_attempt_settled": frozenset(
            {
                "disposition",
                "outcome",
                "input_tokens",
                "output_tokens",
                "cost_usd",
                "reconciled",
            }
        ),
        "question_evidence_persisted": frozenset({"error_code"}),
        "queue_notification": frozenset({"result"}),
        "credential_changed": frozenset({"action", "credential_source", "key_id"}),
        "credential_rejected": frozenset({"reason", "credential_source", "key_id"}),
        "credential_decrypt_failed": frozenset({"reason", "key_id"}),
        "governance_integrity_error": frozenset({"reason"}),
    }
)

_ARCHIVE_V1_NULLABLE_PAYLOAD_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "provider_attempt_reserved": frozenset(
            {"reserved_input_tokens", "reserved_output_tokens", "reserved_cost_usd"}
        ),
        "provider_attempt_settled": frozenset({"input_tokens", "output_tokens", "cost_usd"}),
        "credential_changed": frozenset({"key_id"}),
        "credential_rejected": frozenset({"key_id"}),
        "credential_decrypt_failed": frozenset({"key_id"}),
    }
)

_ARCHIVE_V1_INTEGER_FIELDS = frozenset(
    {
        "policy_version",
        "backlog_count",
        "question_quantum",
        "dispatch_count",
        "responses_added",
        "failed_attempt_count",
        "released_reservations",
        "conservative_settlements",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "input_tokens",
        "output_tokens",
    }
)
_ARCHIVE_V1_DECIMAL_FIELDS = frozenset({"reserved_cost_usd", "cost_usd"})
_ARCHIVE_V1_BOOLEAN_FIELDS = frozenset({"reconciled"})
_ARCHIVE_V1_HASH_FIELDS = frozenset({"policy_hash"})
_ARCHIVE_V1_TIME_FIELDS = frozenset({"not_before"})
_ARCHIVE_V1_ENUM_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "disposition": frozenset({"released_pre_send", "settled_actual", "settled_conservative"}),
        "outcome": frozenset(
            {
                "succeeded",
                "usage_incomplete",
                "transport_error",
                "http_error",
                "provider_response_error",
                "cancelled",
                "mark_send_failed",
                "unexpected_error",
                "released_pre_send",
                "lease_reconciled_pre_send",
                "lease_reconciled_unknown",
            }
        ),
        "reason": frozenset(
            {
                "none",
                "governance_deferred",
                "governance_exhausted",
                "governance_integrity_error",
                "lease_expired",
                "origin_rejected",
                "active_run_conflict",
                "decrypt_failed",
                "worker_error",
            }
        ),
        "status": frozenset({"pending", "running", "completed", "failed", "cancelled"}),
        "result": frozenset({"published", "unavailable", "acknowledged", "already_absent"}),
        "action": frozenset({"created", "replaced", "source_switched", "removed"}),
        "credential_source": frozenset({"none", "environment", "stored"}),
        "error_code": frozenset(
            {"none", "adapter_error", "parse_error", "evaluator_error", "internal_error"}
        ),
    }
)


@dataclass(frozen=True)
class _AuditPayloadContract:
    event_fields: Mapping[str, frozenset[str]]
    nullable_fields: Mapping[str, frozenset[str]]
    integer_fields: frozenset[str]
    decimal_fields: frozenset[str]
    boolean_fields: frozenset[str]
    hash_fields: frozenset[str]
    time_fields: frozenset[str]
    enum_values: Mapping[str, frozenset[str]]


_ARCHIVE_V1_PAYLOAD_CONTRACT = _AuditPayloadContract(
    event_fields=_ARCHIVE_V1_EVENT_PAYLOAD_FIELDS,
    nullable_fields=_ARCHIVE_V1_NULLABLE_PAYLOAD_FIELDS,
    integer_fields=_ARCHIVE_V1_INTEGER_FIELDS,
    decimal_fields=_ARCHIVE_V1_DECIMAL_FIELDS,
    boolean_fields=_ARCHIVE_V1_BOOLEAN_FIELDS,
    hash_fields=_ARCHIVE_V1_HASH_FIELDS,
    time_fields=_ARCHIVE_V1_TIME_FIELDS,
    enum_values=_ARCHIVE_V1_ENUM_VALUES,
)

# The live append/read contract is deliberately a separate copy.  A future
# schema revision can extend it while archive-v1 continues to use the frozen
# values above (or introduces a new archive schema and dispatcher).
_EVENT_PAYLOAD_FIELDS = dict(_ARCHIVE_V1_EVENT_PAYLOAD_FIELDS)
_NULLABLE_PAYLOAD_FIELDS = dict(_ARCHIVE_V1_NULLABLE_PAYLOAD_FIELDS)
_INTEGER_FIELDS = frozenset(_ARCHIVE_V1_INTEGER_FIELDS)
_DECIMAL_FIELDS = frozenset(_ARCHIVE_V1_DECIMAL_FIELDS)
_BOOLEAN_FIELDS = frozenset(_ARCHIVE_V1_BOOLEAN_FIELDS)
_HASH_FIELDS = frozenset(_ARCHIVE_V1_HASH_FIELDS)
_TIME_FIELDS = frozenset(_ARCHIVE_V1_TIME_FIELDS)
_ENUM_VALUES = dict(_ARCHIVE_V1_ENUM_VALUES)
_LIVE_PAYLOAD_CONTRACT = _AuditPayloadContract(
    event_fields=_EVENT_PAYLOAD_FIELDS,
    nullable_fields=_NULLABLE_PAYLOAD_FIELDS,
    integer_fields=_INTEGER_FIELDS,
    decimal_fields=_DECIMAL_FIELDS,
    boolean_fields=_BOOLEAN_FIELDS,
    hash_fields=_HASH_FIELDS,
    time_fields=_TIME_FIELDS,
    enum_values=_ENUM_VALUES,
)


def _normalize_payload(
    event_type: str,
    payload: dict[str, Any] | None,
    *,
    contract: _AuditPayloadContract = _LIVE_PAYLOAD_CONTRACT,
) -> dict[str, Any]:
    allowed = contract.event_fields.get(event_type)
    if allowed is None:
        raise ValueError("unsupported audit event type")
    values = dict(payload or {})
    if set(values) != allowed:
        raise ValueError("audit payload does not match the event contract")

    normalized: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            if key not in contract.nullable_fields.get(event_type, frozenset()):
                raise ValueError(f"audit payload field {key} cannot be null")
            normalized[key] = None
        elif key in contract.integer_fields:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _INT64_MAX
            ):
                raise ValueError(f"audit payload field {key} must be a non-negative integer")
            normalized[key] = value
        elif key in contract.decimal_fields:
            decimal_value = Decimal(str(value))
            rendered = format(decimal_value, "f")
            if (
                not decimal_value.is_finite()
                or decimal_value < 0
                or not _SAFE_DECIMAL.fullmatch(rendered)
            ):
                raise ValueError(f"audit payload field {key} must be a bounded USD decimal")
            normalized[key] = rendered
        elif key in contract.boolean_fields:
            if not isinstance(value, bool):
                raise ValueError(f"audit payload field {key} must be boolean")
            normalized[key] = value
        elif key in contract.hash_fields:
            if not isinstance(value, str) or not _SAFE_HASH.fullmatch(value):
                raise ValueError(f"audit payload field {key} must be a SHA-256 hex digest")
            normalized[key] = value
        elif key in contract.time_fields:
            if isinstance(value, datetime):
                timestamp = value
            elif isinstance(value, str):
                try:
                    timestamp = datetime.fromisoformat(value)
                except ValueError:
                    raise ValueError(f"audit payload field {key} must be a UTC timestamp") from None
            else:
                raise ValueError(f"audit payload field {key} must be a UTC timestamp")
            if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
                raise ValueError(f"audit payload field {key} must be a UTC timestamp")
            normalized[key] = timestamp.isoformat()
        else:
            if (
                not isinstance(value, str)
                or not _SAFE_TOKEN.fullmatch(value)
                or "://" in value
                or normalize_provider_metadata(value, max_length=128) is None
            ):
                raise ValueError(f"audit payload field {key} must be a short safe token")
            allowed_values = contract.enum_values.get(key)
            if allowed_values is not None and value not in allowed_values:
                raise ValueError(f"audit payload field {key} is not an allowed value")
            normalized[key] = value
    return normalized


def _canonical_payload(payload: dict[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _event_matches(event: AuditEvent, values: dict[str, Any]) -> bool:
    """Compare stable business facts represented by an idempotency key.

    ``occurred_at`` and its derived ``expires_at`` are observations made by the
    first successful append.  A retry may recompute them later without changing
    the underlying event identity, so they intentionally do not participate in
    replay conflict detection.
    """

    return bool(
        event.event_type == values["event_type"]
        and hmac.compare_digest(event.payload_hash, values["payload_hash"])
        and dict(event.payload) == values["payload"]
        and event.retention_class == values["retention_class"]
        and event.correlation_id == values["correlation_id"]
        and event.run_id == values["run_id"]
        and event.model_id == values["model_id"]
        and event.question_id == values["question_id"]
        and event.worker_id == values["worker_id"]
        and event.reservation_id == values["reservation_id"]
        and event.attempt == values["attempt"]
        and event.provider_attempt == values["provider_attempt"]
        and event.lease_token == values["lease_token"]
        and event.duration_ms == values["duration_ms"]
    )


def _validate_audit_payload_for_read(
    event_type: object,
    payload: object,
    payload_hash: object,
    *,
    contract: _AuditPayloadContract,
) -> dict[str, Any]:
    try:
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            raise ValueError("invalid audit event shape")
        normalized = _normalize_payload(event_type, payload, contract=contract)
        encoded, expected_hash = _canonical_payload(normalized)
        raw_encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if (
            raw_encoded != encoded
            or not isinstance(payload_hash, str)
            or _SAFE_HASH.fullmatch(payload_hash) is None
            or not hmac.compare_digest(expected_hash, payload_hash)
        ):
            raise ValueError("invalid audit payload hash")
    except Exception:
        raise AuditIntegrityError("retained audit event failed integrity validation") from None
    return normalized


def validate_audit_payload_for_read(
    event_type: object,
    payload: object,
    payload_hash: object,
) -> dict[str, Any]:
    """Validate one canonical retained payload before a live read boundary.

    Rows normally reach the table only through :func:`append_audit_event`, but
    this read-side check fails closed if an import, manual mutation, or storage
    corruption introduced a non-canonical representation or arbitrary text.
    The error never incorporates the rejected value.
    """

    return _validate_audit_payload_for_read(
        event_type,
        payload,
        payload_hash,
        contract=_LIVE_PAYLOAD_CONTRACT,
    )


def _validate_audit_archive_v1_payload_for_read(
    event_type: object,
    payload: object,
    payload_hash: object,
) -> dict[str, Any]:
    """Validate a payload against the immutable archive-v1 event contract."""

    return _validate_audit_payload_for_read(
        event_type,
        payload,
        payload_hash,
        contract=_ARCHIVE_V1_PAYLOAD_CONTRACT,
    )


def validate_audit_identity_for_read(
    name: str,
    value: object,
    *,
    maximum: int,
) -> str | None:
    """Validate one optional non-secret audit identity without reflecting it."""

    try:
        if value is not None and not isinstance(value, str):
            raise ValueError("invalid audit identity type")
        return _safe_identity(name, value, maximum=maximum)
    except Exception:
        raise AuditIntegrityError("retained audit event failed integrity validation") from None


def _safe_identity(name: str, value: str | None, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")
    if (
        not _SAFE_IDENTITY_TOKEN.fullmatch(value)
        or "://" in value
        or normalize_provider_metadata(value, max_length=maximum) is None
    ):
        raise ValueError(f"{name} must be a safe identifier")
    return value


def _validated_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("invalid audit timestamp")
    if value.tzinfo is None:
        # UTCDateTime restores SQLite values as aware UTC.  Treat a naive value
        # from a compatible driver as UTC rather than letting host local time
        # influence retained evidence.
        return value.replace(tzinfo=UTC)
    if value.utcoffset() != timedelta(0):
        return value.astimezone(UTC)
    return value


_ARCHIVE_V1_RETENTION_DAYS: Mapping[str, int] = MappingProxyType(
    {AuditRetentionClass.OPERATIONAL.value: 90, AuditRetentionClass.SECURITY.value: 365}
)
_LIVE_RETENTION_DAYS = dict(_ARCHIVE_V1_RETENTION_DAYS)


def _validate_audit_event_values_for_read(
    *,
    id: object,
    event_key: object,
    event_type: object,
    payload_hash: object,
    payload: object,
    retention_class: object,
    occurred_at: object,
    expires_at: object,
    correlation_id: object,
    run_id: object,
    model_id: object,
    question_id: object,
    worker_id: object,
    reservation_id: object,
    attempt: object,
    provider_attempt: object,
    lease_token: object,
    duration_ms: object,
    payload_validator: Callable[[object, object, object], dict[str, Any]],
    retention_days: Mapping[str, int],
) -> AuditEventReadFacts:
    try:
        normalized_payload = payload_validator(event_type, payload, payload_hash)
        validated_id = validate_audit_identity_for_read("id", id, maximum=36)
        validated_key = validate_audit_identity_for_read("event_key", event_key, maximum=255)
        if validated_id is None or validated_key is None or not isinstance(event_type, str):
            raise ValueError("required audit identity is missing")
        if not isinstance(payload_hash, str):
            raise ValueError("invalid payload hash")
        if not isinstance(retention_class, AuditRetentionClass):
            raise ValueError("invalid audit retention class")
        minimum_days = retention_days.get(retention_class.value)
        if minimum_days is None:
            raise ValueError("unsupported audit retention class")

        occurred = _validated_utc_timestamp(occurred_at)
        expires = _validated_utc_timestamp(expires_at)
        minimum_retention = timedelta(days=minimum_days)
        if expires - occurred < minimum_retention:
            raise ValueError("invalid audit retention interval")

        if isinstance(attempt, bool) or (
            attempt is not None and (not isinstance(attempt, int) or not 0 <= attempt <= _INT32_MAX)
        ):
            raise ValueError("invalid audit attempt")
        if isinstance(provider_attempt, bool) or (
            provider_attempt is not None
            and (not isinstance(provider_attempt, int) or not 1 <= provider_attempt <= _INT32_MAX)
        ):
            raise ValueError("invalid audit provider attempt")
        if isinstance(lease_token, bool) or (
            lease_token is not None
            and (not isinstance(lease_token, int) or not 0 <= lease_token <= _INT64_MAX)
        ):
            raise ValueError("invalid audit lease token")
        if isinstance(duration_ms, bool) or (
            duration_ms is not None
            and (
                not isinstance(duration_ms, (int, float))
                or not math.isfinite(duration_ms)
                or duration_ms < 0
            )
        ):
            raise ValueError("invalid audit duration")

        duration = None if duration_ms is None else float(duration_ms)
        if duration == 0:
            duration = 0.0
        return AuditEventReadFacts(
            id=validated_id,
            event_key=validated_key,
            event_type=event_type,
            payload_hash=payload_hash,
            payload=normalized_payload,
            retention_class=retention_class,
            occurred_at=occurred,
            expires_at=expires,
            correlation_id=validate_audit_identity_for_read(
                "correlation_id", correlation_id, maximum=128
            ),
            run_id=validate_audit_identity_for_read("run_id", run_id, maximum=36),
            model_id=validate_audit_identity_for_read("model_id", model_id, maximum=36),
            question_id=validate_audit_identity_for_read("question_id", question_id, maximum=36),
            worker_id=validate_audit_identity_for_read("worker_id", worker_id, maximum=128),
            reservation_id=validate_audit_identity_for_read(
                "reservation_id", reservation_id, maximum=36
            ),
            attempt=attempt,
            provider_attempt=provider_attempt,
            lease_token=lease_token,
            duration_ms=duration,
        )
    except AuditIntegrityError:
        raise
    except (OverflowError, TypeError, ValueError):
        raise AuditIntegrityError("retained audit event failed integrity validation") from None


def validate_audit_event_values_for_read(
    *,
    id: object,
    event_key: object,
    event_type: object,
    payload_hash: object,
    payload: object,
    retention_class: object,
    occurred_at: object,
    expires_at: object,
    correlation_id: object,
    run_id: object,
    model_id: object,
    question_id: object,
    worker_id: object,
    reservation_id: object,
    attempt: object,
    provider_attempt: object,
    lease_token: object,
    duration_ms: object,
) -> AuditEventReadFacts:
    """Validate every storage field before retained evidence is consumed.

    The returned projection is also the exact comparison contract used by the
    live application.  Rejected values are never included in the raised error.
    """

    return _validate_audit_event_values_for_read(
        id=id,
        event_key=event_key,
        event_type=event_type,
        payload_hash=payload_hash,
        payload=payload,
        retention_class=retention_class,
        occurred_at=occurred_at,
        expires_at=expires_at,
        correlation_id=correlation_id,
        run_id=run_id,
        model_id=model_id,
        question_id=question_id,
        worker_id=worker_id,
        reservation_id=reservation_id,
        attempt=attempt,
        provider_attempt=provider_attempt,
        lease_token=lease_token,
        duration_ms=duration_ms,
        payload_validator=validate_audit_payload_for_read,
        retention_days=_LIVE_RETENTION_DAYS,
    )


def validate_audit_archive_v1_event_values_for_read(
    *,
    id: object,
    event_key: object,
    event_type: object,
    payload_hash: object,
    payload: object,
    retention_class: object,
    occurred_at: object,
    expires_at: object,
    correlation_id: object,
    run_id: object,
    model_id: object,
    question_id: object,
    worker_id: object,
    reservation_id: object,
    attempt: object,
    provider_attempt: object,
    lease_token: object,
    duration_ms: object,
) -> AuditEventReadFacts:
    """Validate complete storage facts against the frozen archive-v1 contract."""

    return _validate_audit_event_values_for_read(
        id=id,
        event_key=event_key,
        event_type=event_type,
        payload_hash=payload_hash,
        payload=payload,
        retention_class=retention_class,
        occurred_at=occurred_at,
        expires_at=expires_at,
        correlation_id=correlation_id,
        run_id=run_id,
        model_id=model_id,
        question_id=question_id,
        worker_id=worker_id,
        reservation_id=reservation_id,
        attempt=attempt,
        provider_attempt=provider_attempt,
        lease_token=lease_token,
        duration_ms=duration_ms,
        payload_validator=_validate_audit_archive_v1_payload_for_read,
        retention_days=_ARCHIVE_V1_RETENTION_DAYS,
    )


def validate_audit_event_for_read(event: AuditEvent) -> AuditEventReadFacts:
    """Return the complete validated storage projection for one ORM row."""

    return validate_audit_event_values_for_read(
        id=event.id,
        event_key=event.event_key,
        event_type=event.event_type,
        payload_hash=event.payload_hash,
        payload=event.payload,
        retention_class=event.retention_class,
        occurred_at=event.occurred_at,
        expires_at=event.expires_at,
        correlation_id=event.correlation_id,
        run_id=event.run_id,
        model_id=event.model_id,
        question_id=event.question_id,
        worker_id=event.worker_id,
        reservation_id=event.reservation_id,
        attempt=event.attempt,
        provider_attempt=event.provider_attempt,
        lease_token=event.lease_token,
        duration_ms=event.duration_ms,
    )


def append_audit_event(
    session: Session,
    *,
    event_key: str,
    event_type: str,
    occurred_at: datetime,
    payload: dict[str, Any] | None = None,
    retention_class: AuditRetentionClass = AuditRetentionClass.OPERATIONAL,
    correlation_id: str | None = None,
    run_id: str | None = None,
    model_id: str | None = None,
    question_id: str | None = None,
    worker_id: str | None = None,
    reservation_id: str | None = None,
    attempt: int | None = None,
    provider_attempt: int | None = None,
    lease_token: int | None = None,
    duration_ms: float | None = None,
) -> AuditEvent:
    """Append or idempotently replay one typed event in the caller transaction."""

    key = _safe_identity("event_key", event_key, maximum=255)
    if key is None:  # Defensive; event_key is statically non-optional.
        raise ValueError("event_key is required")
    if event_type not in _EVENT_PAYLOAD_FIELDS:
        raise ValueError("unsupported audit event type")
    normalized = _normalize_payload(event_type, payload)
    _encoded, payload_hash = _canonical_payload(normalized)

    if isinstance(attempt, bool) or (
        attempt is not None and (not isinstance(attempt, int) or not 0 <= attempt <= _INT32_MAX)
    ):
        raise ValueError("attempt must be non-negative")
    if isinstance(provider_attempt, bool) or (
        provider_attempt is not None
        and (not isinstance(provider_attempt, int) or not 1 <= provider_attempt <= _INT32_MAX)
    ):
        raise ValueError("provider_attempt must be positive")
    if isinstance(lease_token, bool) or (
        lease_token is not None
        and (not isinstance(lease_token, int) or not 0 <= lease_token <= _INT64_MAX)
    ):
        raise ValueError("lease_token must be non-negative")
    if isinstance(duration_ms, bool) or (
        duration_ms is not None
        and (
            not isinstance(duration_ms, (int, float))
            or not math.isfinite(duration_ms)
            or duration_ms < 0
        )
    ):
        raise ValueError("duration_ms must be non-negative")
    if occurred_at.tzinfo is None or occurred_at.utcoffset() != timedelta(0):
        raise ValueError("occurred_at must be a UTC timestamp")
    retention = timedelta(days=365 if retention_class == AuditRetentionClass.SECURITY else 90)
    values = {
        "id": str(uuid4()),
        "event_key": key,
        "event_type": event_type,
        "payload_hash": payload_hash,
        "payload": normalized,
        "retention_class": retention_class,
        "occurred_at": occurred_at,
        "expires_at": occurred_at + retention,
        "correlation_id": _safe_identity("correlation_id", correlation_id, maximum=128),
        "run_id": _safe_identity("run_id", run_id, maximum=36),
        "model_id": _safe_identity("model_id", model_id, maximum=36),
        "question_id": _safe_identity("question_id", question_id, maximum=36),
        "worker_id": _safe_identity("worker_id", worker_id, maximum=128),
        "reservation_id": _safe_identity("reservation_id", reservation_id, maximum=36),
        "attempt": attempt,
        "provider_attempt": provider_attempt,
        "lease_token": lease_token,
        "duration_ms": duration_ms,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = (
            postgresql_insert(AuditEvent.__table__)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["event_key"])
        )
    elif dialect == "sqlite":
        statement = (
            sqlite_insert(AuditEvent.__table__)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["event_key"])
        )
    else:  # The supported production/development dialects are handled above.
        existing = session.scalar(select(AuditEvent).where(AuditEvent.event_key == key))
        if existing is not None:
            if not _event_matches(existing, values):
                raise AuditIntegrityError("audit event key conflicts with a different fact")
            return existing
        statement = AuditEvent.__table__.insert().values(**values)

    session.execute(statement)
    event = session.scalar(select(AuditEvent).where(AuditEvent.event_key == key))
    if event is None:
        raise AuditIntegrityError("audit event could not be read after append")
    if not _event_matches(event, values):
        raise AuditIntegrityError("audit event key conflicts with a different fact")
    return event
