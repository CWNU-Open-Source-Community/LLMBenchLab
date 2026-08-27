"""Encrypted, write-only provider credential owned by one registered model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.model import Model


class ModelCredential(TimestampMixin, Base):
    """An authenticated ciphertext; plaintext is never mapped or persisted."""

    __tablename__ = "model_credentials"
    __table_args__ = (
        CheckConstraint(
            "algorithm = 'aes-256-gcm-v1'",
            name="algorithm_supported",
        ),
    )

    model_id: Mapped[str] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"),
        primary_key=True,
    )
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary(16384), nullable=False)

    model: Mapped[Model] = relationship(back_populates="credential")
