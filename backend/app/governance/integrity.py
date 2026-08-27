"""Durable, secret-free evidence for detected governance corruption."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.db.clock import database_utc_now

from .audit import append_audit_event


def record_governance_integrity_event(
    session_factory: sessionmaker[Session],
    *,
    run_id: str | None = None,
    model_id: str | None = None,
    worker_id: str | None = None,
) -> None:
    """Append one independent event without reflecting the detected bad value.

    Callers invoke this only after the transaction that detected the mismatch has
    rolled back. The event intentionally records a fixed reason instead of an
    exception message, ledger value, URL, payload, or credential material.
    """

    with session_factory() as session, session.begin():
        now = database_utc_now(session)
        append_audit_event(
            session,
            event_key=f"governance-integrity:{uuid4()}",
            event_type="governance_integrity_error",
            occurred_at=now,
            payload={"reason": "governance_integrity_error"},
            correlation_id=run_id,
            run_id=run_id,
            model_id=model_id,
            worker_id=worker_id,
        )
