"""Registered model configuration."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Enum,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import CredentialSource, ProviderType
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.evaluation_run import EvaluationRun
    from app.models.model_credential import ModelCredential


def _uuid() -> str:
    return str(uuid4())


class Model(TimestampMixin, Base):
    """A provider configuration with environment or encrypted credential source."""

    __tablename__ = "models"
    __table_args__ = (
        UniqueConstraint("name", name="uq_models_name"),
        CheckConstraint(
            "provider_type IN ('mock', 'openai_compatible', 'openai_responses', "
            "'anthropic_messages')",
            name="provider_type_values",
        ),
        CheckConstraint(
            "credential_source IN ('none', 'environment', 'stored')",
            name="credential_source_values",
        ),
        CheckConstraint(
            "provider_type NOT IN "
            "('openai_compatible', 'openai_responses', 'anthropic_messages') OR "
            "(base_url IS NOT NULL AND remote_model_name IS NOT NULL AND "
            "((credential_source = 'environment' AND api_key_env IS NOT NULL) OR "
            "(credential_source = 'stored' AND api_key_env IS NULL)))",
            name="openai_configuration_required",
        ),
        CheckConstraint(
            "provider_type != 'mock' OR "
            "(base_url IS NULL AND remote_model_name IS NULL AND api_key_env IS NULL "
            "AND credential_source = 'none')",
            name="mock_configuration_empty",
        ),
        CheckConstraint("input_price_per_million >= 0", name="input_price_nonnegative"),
        CheckConstraint("output_price_per_million >= 0", name="output_price_nonnegative"),
        Index("ix_models_provider_enabled", "provider_type", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_type: Mapped[ProviderType] = mapped_column(
        Enum(
            ProviderType,
            name="provider_type",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        nullable=False,
        index=True,
    )
    base_url: Mapped[str | None] = mapped_column(String(2048))
    remote_model_name: Mapped[str | None] = mapped_column(String(256))
    api_key_env: Mapped[str | None] = mapped_column(String(128))
    credential_source: Mapped[CredentialSource] = mapped_column(
        Enum(
            CredentialSource,
            name="credential_source",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=CredentialSource.NONE,
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    input_price_per_million: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    output_price_per_million: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    default_parameters: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )

    runs: Mapped[list[EvaluationRun]] = relationship(back_populates="model")
    credential: Mapped[ModelCredential | None] = relationship(
        back_populates="model",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    @property
    def has_api_key(self) -> bool:
        """Whether this model owns an encrypted, application-managed API key."""

        return self.credential_source == CredentialSource.STORED and self.credential is not None
