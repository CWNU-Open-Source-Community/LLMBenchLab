"""Add encrypted, write-only Web provider credentials.

Revision ID: 20260827_0003
Revises: 20260825_0002
Create Date: 2026-08-27 14:00:00 UTC
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260827_0003"
down_revision: str | None = "20260825_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "models",
        sa.Column("credential_source", sa.String(length=11), nullable=True),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE models SET credential_source = CASE "
            "WHEN provider_type = 'openai_compatible' THEN 'environment' ELSE 'none' END"
        )
    )

    with op.batch_alter_table("models") as batch_op:
        batch_op.drop_constraint(op.f("ck_models_openai_configuration_required"), type_="check")
        batch_op.drop_constraint(op.f("ck_models_mock_configuration_empty"), type_="check")
        batch_op.alter_column(
            "credential_source",
            existing_type=sa.String(length=11),
            nullable=False,
        )
        batch_op.create_check_constraint(
            op.f("ck_models_credential_source_values"),
            "credential_source IN ('none', 'environment', 'stored')",
        )
        batch_op.create_check_constraint(
            op.f("ck_models_openai_configuration_required"),
            "provider_type != 'openai_compatible' OR "
            "(base_url IS NOT NULL AND remote_model_name IS NOT NULL AND "
            "((credential_source = 'environment' AND api_key_env IS NOT NULL) OR "
            "(credential_source = 'stored' AND api_key_env IS NULL)))",
        )
        batch_op.create_check_constraint(
            op.f("ck_models_mock_configuration_empty"),
            "provider_type != 'mock' OR "
            "(base_url IS NULL AND remote_model_name IS NULL AND api_key_env IS NULL "
            "AND credential_source = 'none')",
        )

    op.create_table(
        "model_credentials",
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(length=16384), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "algorithm = 'aes-256-gcm-v1'",
            name=op.f("ck_model_credentials_algorithm_supported"),
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_model_credentials_model_id_models"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("model_id", name=op.f("pk_model_credentials")),
    )


def downgrade() -> None:
    connection = op.get_bind()
    encrypted_credentials = connection.execute(
        sa.text("SELECT COUNT(*) FROM model_credentials")
    ).scalar_one()
    if encrypted_credentials:
        raise RuntimeError(
            "Cannot downgrade while encrypted Web credentials exist; convert or remove "
            "stored provider credentials first"
        )

    op.drop_table("model_credentials")
    with op.batch_alter_table("models") as batch_op:
        batch_op.drop_constraint(op.f("ck_models_mock_configuration_empty"), type_="check")
        batch_op.drop_constraint(op.f("ck_models_openai_configuration_required"), type_="check")
        batch_op.drop_constraint(op.f("ck_models_credential_source_values"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_models_openai_configuration_required"),
            "provider_type != 'openai_compatible' OR "
            "(base_url IS NOT NULL AND remote_model_name IS NOT NULL AND api_key_env IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            op.f("ck_models_mock_configuration_empty"),
            "provider_type != 'mock' OR "
            "(base_url IS NULL AND remote_model_name IS NULL AND api_key_env IS NULL)",
        )
        batch_op.drop_column("credential_source")
