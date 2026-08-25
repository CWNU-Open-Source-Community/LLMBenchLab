"""Reusable ORM mixins."""

from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utc_now
from app.db.types import UTCDateTime


class TimestampMixin:
    """UTC creation and modification timestamps."""

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )
