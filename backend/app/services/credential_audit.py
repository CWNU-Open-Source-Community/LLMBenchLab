"""Secret-minimized audit helpers for Provider credential lifecycle events."""

from __future__ import annotations

import re
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.logging import get_request_id
from app.db.clock import database_utc_now
from app.models import AuditRetentionClass, CredentialSource
from app.security.provider_metadata import normalize_provider_metadata

_SAFE_AUDIT_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def safe_credential_key_id(value: object) -> str | None:
    """Return a short identifier suitable for audit payloads, or fail closed."""

    normalized = normalize_provider_metadata(value, max_length=64)
    if normalized is None or _SAFE_AUDIT_KEY_ID.fullmatch(normalized) is None:
        return None
    return normalized


def _append_credential_audit_event(
    session: Session,
    *,
    model_id: str,
    event_type: str,
    payload: dict[str, str | None],
) -> None:
    """Append one typed, secret-free credential event in the caller transaction."""

    # Keep API and Worker import boundaries lightweight.  The governance
    # package is loaded only when a credential event actually needs writing.
    from app.governance.audit import append_audit_event

    request_id = get_request_id()
    event_nonce = request_id or str(uuid4())
    append_audit_event(
        session,
        event_key=f"credential:{model_id}:{event_nonce}:{event_type}",
        event_type=event_type,
        occurred_at=database_utc_now(session),
        payload=payload,
        retention_class=AuditRetentionClass.SECURITY,
        correlation_id=request_id or event_nonce,
        model_id=model_id,
    )


def _append_credential_audit_after_rollback(
    session: Session,
    *,
    model_id: str,
    event_type: str,
    payload: dict[str, str | None],
) -> None:
    """Release current locks, then durably append an event in a short transaction."""

    bind = session.get_bind()
    session.rollback()
    with (
        Session(bind=bind, autoflush=False, expire_on_commit=False) as audit_session,
        audit_session.begin(),
    ):
        _append_credential_audit_event(
            audit_session,
            model_id=model_id,
            event_type=event_type,
            payload=payload,
        )


def audit_credential_changed(
    session: Session,
    *,
    model_id: str,
    action: str,
    source: CredentialSource,
    key_id: str | None,
) -> None:
    """Record one committed credential lifecycle change."""

    _append_credential_audit_event(
        session,
        model_id=model_id,
        event_type="credential_changed",
        payload={
            "action": action,
            "credential_source": source.value,
            "key_id": safe_credential_key_id(key_id),
        },
    )


def audit_credential_rejected_after_rollback(
    session: Session,
    *,
    model_id: str,
    reason: str,
    source: CredentialSource,
    key_id: str | None,
) -> None:
    """Record a rejected mutation after releasing its CRUD transaction."""

    _append_credential_audit_after_rollback(
        session,
        model_id=model_id,
        event_type="credential_rejected",
        payload={
            "reason": reason,
            "credential_source": source.value,
            "key_id": safe_credential_key_id(key_id),
        },
    )


def audit_credential_decrypt_failed(
    session: Session,
    *,
    model_id: str,
    key_id: str | None,
    after_rollback: bool,
) -> None:
    """Record a fixed decrypt-failure code without exception or envelope data."""

    writer = (
        _append_credential_audit_after_rollback
        if after_rollback
        else _append_credential_audit_event
    )
    writer(
        session,
        model_id=model_id,
        event_type="credential_decrypt_failed",
        payload={"reason": "decrypt_failed", "key_id": safe_credential_key_id(key_id)},
    )


def credential_change_action(
    *,
    previous_source: CredentialSource,
    new_source: CredentialSource,
    api_key_replaced: bool,
    environment_name_changed: bool,
) -> str | None:
    """Classify a public credential mutation into the typed audit vocabulary."""

    if previous_source != new_source:
        return "removed" if new_source == CredentialSource.NONE else "source_switched"
    if api_key_replaced or (
        new_source == CredentialSource.ENVIRONMENT and environment_name_changed
    ):
        return "replaced"
    return None
