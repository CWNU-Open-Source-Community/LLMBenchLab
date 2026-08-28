"""Database-authoritative UTC clock helpers for persisted execution facts."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session


def as_utc(value: datetime) -> datetime:
    """Normalize a database timestamp to an aware UTC value."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def database_utc_now(session: Session) -> datetime:
    """Read the database clock used to order cross-process persisted facts."""

    value = session.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("database_clock_unavailable")
    return as_utc(value)
