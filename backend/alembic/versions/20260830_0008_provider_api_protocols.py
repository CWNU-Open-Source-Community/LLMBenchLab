"""Expand registered Model provider types to explicit API protocols.

Revision ID: 20260830_0008
Revises: 20260830_0007
Create Date: 2026-08-30 12:00:00 UTC
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_0008"
down_revision: str | None = "20260830_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_PROVIDER_VALUES = "provider_type IN ('mock', 'openai_compatible')"
_NEW_PROVIDER_VALUES = (
    "provider_type IN ('mock', 'openai_compatible', 'openai_responses', 'anthropic_messages')"
)
_OLD_REMOTE_CONFIGURATION = (
    "provider_type != 'openai_compatible' OR "
    "(base_url IS NOT NULL AND remote_model_name IS NOT NULL AND "
    "((credential_source = 'environment' AND api_key_env IS NOT NULL) OR "
    "(credential_source = 'stored' AND api_key_env IS NULL)))"
)
_NEW_REMOTE_CONFIGURATION = (
    "provider_type NOT IN "
    "('openai_compatible', 'openai_responses', 'anthropic_messages') OR "
    "(base_url IS NOT NULL AND remote_model_name IS NOT NULL AND "
    "((credential_source = 'environment' AND api_key_env IS NOT NULL) OR "
    "(credential_source = 'stored' AND api_key_env IS NULL)))"
)


def _replace_constraints(
    *,
    provider_values: str,
    remote_configuration: str,
    existing_length: int,
    target_length: int,
) -> None:
    with op.batch_alter_table("models") as batch_op:
        batch_op.drop_constraint(op.f("ck_models_provider_type_values"), type_="check")
        batch_op.drop_constraint(
            op.f("ck_models_openai_configuration_required"),
            type_="check",
        )
        batch_op.alter_column(
            "provider_type",
            existing_type=sa.String(length=existing_length),
            type_=sa.String(length=target_length),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            op.f("ck_models_provider_type_values"),
            provider_values,
        )
        batch_op.create_check_constraint(
            op.f("ck_models_openai_configuration_required"),
            remote_configuration,
        )


def upgrade() -> None:
    _replace_constraints(
        provider_values=_NEW_PROVIDER_VALUES,
        remote_configuration=_NEW_REMOTE_CONFIGURATION,
        existing_length=17,
        target_length=18,
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif connection.dialect.name == "postgresql":
        connection.exec_driver_sql("LOCK TABLE models IN ACCESS EXCLUSIVE MODE")
    new_type_count = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM models "
            "WHERE provider_type IN ('openai_responses', 'anthropic_messages')"
        )
    )
    if int(new_type_count or 0) != 0:
        raise RuntimeError(
            "Cannot downgrade while openai_responses or anthropic_messages Models exist; "
            "delete or convert those Model configurations first"
        )
    _replace_constraints(
        provider_values=_OLD_PROVIDER_VALUES,
        remote_configuration=_OLD_REMOTE_CONFIGURATION,
        existing_length=18,
        target_length=17,
    )
