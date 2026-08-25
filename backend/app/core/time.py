"""UTC time helpers used by persistence and API layers."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return an aware UTC timestamp.

    A function (rather than an evaluated module constant) is intentionally used as
    the SQLAlchemy default so every row receives its own timestamp.
    """

    return datetime.now(UTC)
