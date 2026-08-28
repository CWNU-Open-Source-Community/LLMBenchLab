"""Safely adopt supported unversioned databases before Alembic upgrades."""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine
from sqlalchemy.engine import Connection, make_url

from app import models as _models  # noqa: F401 -- registers all mapped tables
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import create_database_engine

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_CONFIG_PATH = BACKEND_ROOT / "alembic.ini"
LEGACY_REVISION = "20260824_0000"
PHASE_1_REVISION = "20260824_0001"
RELIABILITY_REVISION = "20260825_0002"
CREDENTIAL_REVISION = "20260827_0003"
GOVERNANCE_REVISION = "20260827_0004"

_WORKER_PROGRESS_TABLES = {"worker_processes"}
_WORKER_PROGRESS_INDEXES = {
    "add_index:audit_events.ix_audit_events_expires_id",
    "add_index:audit_events.ix_audit_events_occurred_id",
    "add_index:worker_processes.ix_worker_processes_stopped_seen_generation",
}
_WORKER_PROGRESS_DIFFERENCE_SET = {
    f"add_table:{table_name}" for table_name in _WORKER_PROGRESS_TABLES
} | _WORKER_PROGRESS_INDEXES
_WORKER_PROGRESS_DIFFERENCES = tuple(sorted(_WORKER_PROGRESS_DIFFERENCE_SET))
_WORKER_PROGRESS_EXISTING_INDEX_NAMES = {
    "ix_audit_events_expires_id",
    "ix_audit_events_occurred_id",
}

_WEB_CREDENTIAL_DIFFERENCES = {
    "add_table:model_credentials",
    "add_column:models.credential_source",
    "add_constraint:models.ck_models_credential_source_values",
}

_GOVERNANCE_TABLES = {
    "governance_policies",
    "governance_scopes",
    "governance_minute_buckets",
    "question_executions",
    "provider_call_reservations",
    "audit_events",
}
_GOVERNANCE_COLUMNS = {
    "add_column:evaluation_responses.finish_reason",
    "add_column:evaluation_responses.http_attempt_count",
    "add_column:evaluation_responses.provider_request_id",
    "add_column:evaluation_responses.returned_model",
    "add_column:evaluation_responses.system_fingerprint",
    "add_column:evaluation_runs.dispatch_count",
    "add_column:evaluation_runs.failed_attempt_count",
    "add_column:evaluation_runs.governance_not_before",
    "add_column:evaluation_runs.governance_policy_id",
    "add_column:evaluation_runs.governance_reason",
    "add_column:evaluation_runs.governance_status",
    "add_column:evaluation_runs.input_token_reservation",
    "add_column:evaluation_runs.last_scheduled_at",
    "add_column:evaluation_runs.lifetime_cost_budget_usd",
    "add_column:evaluation_runs.lifetime_request_budget",
    "add_column:evaluation_runs.lifetime_token_budget",
}
_GOVERNANCE_NEW_CONSTRAINTS = {
    "add_constraint:evaluation_responses.ck_evaluation_responses_http_attempt_count_positive",
    "add_constraint:evaluation_runs.ck_evaluation_runs_dispatch_count_nonnegative",
    "add_constraint:evaluation_runs.ck_evaluation_runs_failed_attempt_count_nonnegative",
    "add_constraint:evaluation_runs.ck_evaluation_runs_failed_attempt_count_within_limit",
    "add_constraint:evaluation_runs.ck_evaluation_runs_governance_delay_matches_pending",
    "add_constraint:evaluation_runs.ck_evaluation_runs_governance_exhausted_is_failed",
    "add_constraint:evaluation_runs.ck_evaluation_runs_governance_policy_matches_status",
    "add_constraint:evaluation_runs.ck_evaluation_runs_governance_status_values",
    "add_constraint:evaluation_runs.ck_evaluation_runs_input_token_reservation_nonnegative",
    "add_constraint:evaluation_runs.ck_evaluation_runs_lifetime_cost_budget_nonnegative",
    "add_constraint:evaluation_runs.ck_evaluation_runs_lifetime_request_budget_nonnegative",
    "add_constraint:evaluation_runs.ck_evaluation_runs_lifetime_token_budget_nonnegative",
}
_GOVERNANCE_EXISTING_INDEXES = {
    "add_index:evaluation_runs.ix_evaluation_runs_finished_at_id",
    "add_index:evaluation_runs.ix_evaluation_runs_governance_dispatch",
    "add_index:evaluation_runs.ix_evaluation_runs_governance_policy_id",
    "add_index:evaluation_runs.ix_evaluation_runs_started_at_id",
}
_GOVERNANCE_INDEXES = _GOVERNANCE_EXISTING_INDEXES | {
    "add_index:audit_events.ix_audit_events_expiry",
    "add_index:audit_events.ix_audit_events_run_occurred",
    "add_index:audit_events.ix_audit_events_type_occurred",
    "add_index:governance_minute_buckets.ix_governance_minute_buckets_window",
    "add_index:governance_policies.ix_governance_policies_created",
    "add_index:governance_policies.uq_governance_policies_single_active",
    "add_index:governance_scopes.ix_governance_scopes_overdrawn",
    "add_index:governance_scopes.ix_governance_scopes_type_key",
    "add_index:provider_call_reservations.ix_provider_call_reservations_model_id",
    "add_index:provider_call_reservations.ix_provider_call_reservations_provider_scope",
    "add_index:provider_call_reservations.ix_provider_call_reservations_question_id",
    "add_index:provider_call_reservations.ix_provider_call_reservations_run_created",
    "add_index:provider_call_reservations.ix_provider_call_reservations_run_id",
    "add_index:provider_call_reservations.ix_provider_call_reservations_state_lease",
    "add_index:question_executions.ix_question_executions_question_id",
    "add_index:question_executions.ix_question_executions_retry_due",
    "add_index:question_executions.ix_question_executions_run_id",
}
_GOVERNANCE_DIFFERENCE_SET = (
    _GOVERNANCE_COLUMNS
    | _GOVERNANCE_NEW_CONSTRAINTS
    | _GOVERNANCE_INDEXES
    | {f"add_table:{table_name}" for table_name in _GOVERNANCE_TABLES}
    | {
        "add_fk:evaluation_runs.fk_evaluation_runs_governance_policy_id_governance_policies",
        "remove_constraint:evaluation_runs.ck_evaluation_runs_attempt_within_limit",
    }
    | _WORKER_PROGRESS_DIFFERENCE_SET
)
_GOVERNANCE_DIFFERENCES = tuple(sorted(_GOVERNANCE_DIFFERENCE_SET))
_GOVERNANCE_CHECK_NAMES_BY_TABLE = {
    table_name: {
        fingerprint.rsplit(".", 1)[1]
        for fingerprint in _GOVERNANCE_NEW_CONSTRAINTS
        if fingerprint.startswith(f"add_constraint:{table_name}.")
    }
    for table_name in ("evaluation_runs", "evaluation_responses")
}
_GOVERNANCE_EXISTING_INDEX_NAMES = {
    fingerprint.rsplit(".", 1)[1] for fingerprint in _GOVERNANCE_EXISTING_INDEXES
}
_SUPERSEDED_ATTEMPT_CHECK_DIFFERENCES = {
    "add_constraint:evaluation_runs.ck_evaluation_runs_attempt_within_limit",
    "remove_constraint:evaluation_runs.ck_evaluation_runs_attempt_within_limit",
}

_RELIABILITY_COLUMNS = {
    "add_column:evaluation_runs.attempt_count",
    "add_column:evaluation_runs.max_attempts",
    "add_column:evaluation_runs.lease_owner",
    "add_column:evaluation_runs.lease_token",
    "add_column:evaluation_runs.lease_expires_at",
    "add_column:evaluation_runs.heartbeat_at",
    "add_column:evaluation_runs.next_attempt_at",
    "add_column:evaluation_runs.last_enqueued_at",
    "add_column:evaluation_runs.last_error",
    "add_column:evaluation_runs.dead_lettered_at",
}
_RELIABILITY_CONSTRAINTS = {
    "add_constraint:evaluation_runs.ck_evaluation_runs_attempt_count_nonnegative",
    "add_constraint:evaluation_runs.ck_evaluation_runs_max_attempts_positive",
    "add_constraint:evaluation_runs.ck_evaluation_runs_attempt_within_limit",
    "add_constraint:evaluation_runs.ck_evaluation_runs_lease_token_nonnegative",
    "add_constraint:evaluation_runs.ck_evaluation_runs_lease_matches_running_status",
    "add_constraint:evaluation_runs.ck_evaluation_runs_next_attempt_only_pending",
    "add_constraint:evaluation_runs.ck_evaluation_runs_dead_letter_only_failed",
}
_RELIABILITY_INDEXES = {
    "add_index:evaluation_runs.ix_evaluation_runs_dispatch_due",
    "add_index:evaluation_runs.ix_evaluation_runs_lease_expiry",
}
_RELIABILITY_CHECK_NAMES = {
    fingerprint.rsplit(".", 1)[1] for fingerprint in _RELIABILITY_CONSTRAINTS
}
_RELIABILITY_INDEX_NAMES = {fingerprint.rsplit(".", 1)[1] for fingerprint in _RELIABILITY_INDEXES}
_PHASE_1_DIFFERENCES = tuple(
    sorted(
        (
            _WEB_CREDENTIAL_DIFFERENCES
            | _RELIABILITY_COLUMNS
            | _RELIABILITY_CONSTRAINTS
            | _RELIABILITY_INDEXES
            | _GOVERNANCE_DIFFERENCE_SET
        )
        - _SUPERSEDED_ATTEMPT_CHECK_DIFFERENCES
    )
)

_RELIABILITY_DIFFERENCES = tuple(sorted(_WEB_CREDENTIAL_DIFFERENCES | _GOVERNANCE_DIFFERENCE_SET))

_CREDENTIAL_DIFFERENCES = _GOVERNANCE_DIFFERENCES

_LEGACY_DIFFERENCES = tuple(
    sorted(
        set(_PHASE_1_DIFFERENCES)
        | {
            "modify_nullable:models.input_price_per_million:False->True",
            "modify_nullable:models.output_price_per_million:False->True",
            "add_constraint:models.ck_models_mock_configuration_empty",
            "add_constraint:models.ck_models_openai_configuration_required",
            "add_column:questions.position",
            "add_constraint:questions.uq_questions_benchmark_position",
        }
    )
)


class SchemaPreparationError(RuntimeError):
    """Raised when an unversioned database cannot be adopted without guessing."""


@dataclass(frozen=True)
class PreparationResult:
    """Outcome of the migration preflight."""

    action: str
    backup_path: Path | None = None
    stamped_revision: str | None = None


def _script_directory(config_path: Path = ALEMBIC_CONFIG_PATH) -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(config_path)))


def expected_database_heads(config_path: Path = ALEMBIC_CONFIG_PATH) -> tuple[str, ...]:
    """Return the revisions that a runnable application database must have."""

    return tuple(_script_directory(config_path).get_heads())


def database_heads(connection: Connection) -> tuple[str, ...]:
    """Read applied revisions without creating or changing the version table."""

    return tuple(MigrationContext.configure(connection).get_current_heads())


def stamp_database(
    engine: Engine,
    revision: str,
    *,
    config_path: Path = ALEMBIC_CONFIG_PATH,
) -> None:
    """Use Alembic's stamp machinery on a schema already verified by the caller."""

    with engine.begin() as connection:
        _stamp_connection(connection, revision, config_path=config_path)


def _stamp_connection(
    connection: Connection,
    revision: str,
    *,
    config_path: Path = ALEMBIC_CONFIG_PATH,
) -> None:
    scripts = _script_directory(config_path)
    MigrationContext.configure(connection).stamp(scripts, revision)


def require_database_at_head(
    engine: Engine,
    *,
    config_path: Path = ALEMBIC_CONFIG_PATH,
) -> None:
    """Fail startup early when migrations have not brought the database to head."""

    expected = set(expected_database_heads(config_path))
    with engine.connect() as connection:
        current = set(database_heads(connection))
    if current == expected:
        return
    current_label = ", ".join(sorted(current)) if current else "unversioned"
    expected_label = ", ".join(sorted(expected))
    raise RuntimeError(
        "Database schema is not ready "
        f"(current: {current_label}; expected: {expected_label}). "
        "Run `make setup` or `make migrate` before starting the backend."
    )


def _flatten_differences(differences: list[Any]) -> list[tuple[Any, ...]]:
    flattened: list[tuple[Any, ...]] = []
    for difference in differences:
        if isinstance(difference, list):
            flattened.extend(_flatten_differences(difference))
        else:
            flattened.append(difference)
    return flattened


def _difference_fingerprint(difference: tuple[Any, ...]) -> str:
    operation = str(difference[0])
    if operation == "modify_nullable":
        return f"modify_nullable:{difference[2]}.{difference[3]}:{difference[5]}->{difference[6]}"
    if operation == "add_column":
        column = difference[3]
        return f"add_column:{difference[2]}.{column.name}"
    if operation == "add_constraint":
        constraint = difference[1]
        return f"add_constraint:{constraint.table.name}.{constraint.name}"
    if operation in {"add_fk", "remove_constraint"}:
        constraint = difference[1]
        return f"{operation}:{constraint.table.name}.{constraint.name}"
    if operation == "add_index":
        index = difference[1]
        return f"add_index:{index.table.name}.{index.name}"
    if operation == "add_table":
        table = difference[1]
        return f"add_table:{table.name}"
    return f"unsupported:{operation}"


def _schema_differences(connection: Connection) -> tuple[str, ...]:
    context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": True,
            "compare_server_default": True,
            "render_as_batch": connection.dialect.name == "sqlite",
        },
    )
    differences = compare_metadata(context, Base.metadata)
    return tuple(
        sorted(
            _difference_fingerprint(difference) for difference in _flatten_differences(differences)
        )
    )


def _normalized_check_sql(sqltext: Any) -> str:
    return " ".join(str(sqltext).split())


def _sqlite_index_where(index: sa.Index) -> str:
    where = index.dialect_options["sqlite"].get("where")
    return _normalized_check_sql(where) if where is not None else ""


def _sqlite_sql_tokens(sql: str) -> tuple[str, ...]:
    """Return unquoted SQL tokens so literals and quoted identifiers are ignored."""

    tokens: list[str] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline == -1 else newline + 1
            continue
        if sql.startswith("/*", index):
            closing_comment = sql.find("*/", index + 2)
            index = len(sql) if closing_comment == -1 else closing_comment + 2
            continue
        if character in {"'", '"', "`", "["}:
            closing = "]" if character == "[" else character
            index += 1
            while index < len(sql):
                if sql[index] == closing:
                    if closing != "]" and index + 1 < len(sql) and sql[index + 1] == closing:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if character.isalpha() or character == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append(sql[index:end].upper())
            index = end
            continue
        index += 1
    return tuple(tokens)


def _validate_sqlite_ddl_modifiers(
    connection: Connection,
    *,
    schema_revision: str,
) -> None:
    """Reject SQLite syntax whose semantics are omitted by reflection/autogenerate."""

    expected_tables = _schema_tables_for_revision(schema_revision)
    domain_tables = set(expected_tables)
    supported_partial_indexes = {
        (table.name, str(index.name))
        for table in expected_tables.values()
        for index in table.indexes
        if index.dialect_options["sqlite"].get("where") is not None
    }
    schema_objects = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index') AND sql IS NOT NULL"
    ).mappings()
    forbidden_tokens = {
        "ASC",
        "AUTOINCREMENT",
        "COLLATE",
        "DESC",
        "DEFERRABLE",
        "GENERATED",
        "INITIALLY",
        "MATCH",
    }
    for schema_object in schema_objects:
        if schema_object["tbl_name"] not in domain_tables:
            continue
        tokens = _sqlite_sql_tokens(schema_object["sql"])
        token_pairs = set(pairwise(tokens))
        has_unsupported_modifier = bool(forbidden_tokens & set(tokens)) or bool(
            {("ON", "CONFLICT"), ("ON", "UPDATE")} & token_pairs
        )
        if schema_object["type"] == "index":
            has_unsupported_modifier = has_unsupported_modifier or (
                "WHERE" in tokens
                and (schema_object["tbl_name"], schema_object["name"])
                not in supported_partial_indexes
            )
        if has_unsupported_modifier:
            raise SchemaPreparationError(
                "SQLite DDL modifiers on "
                f"{schema_object['type']} {schema_object['name']} do not match the supported "
                "schema; conflict policies, collations, generated columns, deferred foreign "
                "keys, ordered/partial indexes, and related modifiers cannot be adopted."
            )


def _schema_tables_for_revision(schema_revision: str) -> dict[str, sa.Table]:
    tables = dict(Base.metadata.tables)
    if schema_revision in {
        LEGACY_REVISION,
        PHASE_1_REVISION,
        RELIABILITY_REVISION,
        CREDENTIAL_REVISION,
        GOVERNANCE_REVISION,
    }:
        for table_name in _WORKER_PROGRESS_TABLES:
            tables.pop(table_name, None)
    if schema_revision in {LEGACY_REVISION, PHASE_1_REVISION, RELIABILITY_REVISION}:
        tables.pop("model_credentials", None)
    if schema_revision in {
        LEGACY_REVISION,
        PHASE_1_REVISION,
        RELIABILITY_REVISION,
        CREDENTIAL_REVISION,
    }:
        for table_name in _GOVERNANCE_TABLES:
            tables.pop(table_name, None)
    return tables


def _validate_sqlite_table_options(connection: Connection, *, schema_revision: str) -> None:
    expected_tables = _schema_tables_for_revision(schema_revision)
    table_options = {
        row["name"]: row
        for row in connection.exec_driver_sql("PRAGMA table_list").mappings()
        if row["schema"] == "main" and row["name"] in expected_tables
    }
    for table_name in expected_tables:
        options = table_options.get(table_name)
        columns = connection.exec_driver_sql(f'PRAGMA table_xinfo("{table_name}")').mappings()
        has_hidden_columns = any(bool(column.get("hidden", 0)) for column in columns)
        if (
            options is None
            or options["type"] != "table"
            or bool(options.get("wr", 0))
            or bool(options.get("strict", 0))
            or has_hidden_columns
        ):
            raise SchemaPreparationError(
                f"SQLite table options on {table_name} do not match the supported schema; "
                "WITHOUT ROWID, STRICT, virtual domain tables, and generated columns "
                "cannot be adopted."
            )


def _validate_relational_structure(connection: Connection, *, schema_revision: str) -> None:
    inspector = sa.inspect(connection)
    legacy = schema_revision == LEGACY_REVISION
    pre_reliability = schema_revision in {LEGACY_REVISION, PHASE_1_REVISION}
    pre_governance = schema_revision in {
        LEGACY_REVISION,
        PHASE_1_REVISION,
        RELIABILITY_REVISION,
        CREDENTIAL_REVISION,
    }
    pre_worker_progress = schema_revision in {
        LEGACY_REVISION,
        PHASE_1_REVISION,
        RELIABILITY_REVISION,
        CREDENTIAL_REVISION,
        GOVERNANCE_REVISION,
    }
    for table_name, table in _schema_tables_for_revision(schema_revision).items():
        table_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).scalar_one()
        table_tokens = _sqlite_sql_tokens(table_sql)

        expected_unique_count = sum(
            isinstance(constraint, sa.UniqueConstraint) for constraint in table.constraints
        )
        if legacy and table_name == "questions":
            expected_unique_count -= 1
        expected_foreign_key_count = len(table.foreign_key_constraints)
        if pre_governance and table_name == "evaluation_runs":
            expected_foreign_key_count -= 1
        expected_declaration_counts = (
            int(bool(table.primary_key.columns)),
            expected_unique_count,
            expected_foreign_key_count,
        )
        actual_declaration_counts = (
            sum(pair == ("PRIMARY", "KEY") for pair in pairwise(table_tokens)),
            table_tokens.count("UNIQUE"),
            sum(pair == ("FOREIGN", "KEY") for pair in pairwise(table_tokens)),
        )

        expected_primary_key = (
            str(table.primary_key.name),
            tuple(column.name for column in table.primary_key.columns),
        )
        reflected_primary_key = inspector.get_pk_constraint(table_name)
        actual_primary_key = (
            str(reflected_primary_key["name"]),
            tuple(reflected_primary_key["constrained_columns"]),
        )

        expected_unique_entries = [
            (str(constraint.name), tuple(column.name for column in constraint.columns))
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        ]
        if legacy and table_name == "questions":
            expected_unique_entries = [
                entry
                for entry in expected_unique_entries
                if entry != ("uq_questions_benchmark_position", ("benchmark_id", "position"))
            ]
        expected_uniques = tuple(sorted(expected_unique_entries))
        actual_uniques = tuple(
            sorted(
                (str(constraint["name"]), tuple(constraint["column_names"]))
                for constraint in inspector.get_unique_constraints(table_name)
            )
        )

        expected_foreign_keys = tuple(
            sorted(
                (
                    str(constraint.name),
                    tuple(element.parent.name for element in constraint.elements),
                    constraint.referred_table.name,
                    tuple(element.column.name for element in constraint.elements),
                    (constraint.ondelete or "").upper(),
                )
                for constraint in table.foreign_key_constraints
                if not (
                    pre_governance
                    and table_name == "evaluation_runs"
                    and str(constraint.name)
                    == "fk_evaluation_runs_governance_policy_id_governance_policies"
                )
            )
        )
        actual_foreign_keys = tuple(
            sorted(
                (
                    str(constraint["name"]),
                    tuple(constraint["constrained_columns"]),
                    str(constraint["referred_table"]),
                    tuple(constraint["referred_columns"]),
                    str(constraint["options"].get("ondelete", "")).upper(),
                )
                for constraint in inspector.get_foreign_keys(table_name)
            )
        )

        expected_indexes = {
            (
                str(index.name),
                bool(index.unique),
                tuple(column.name for column in index.columns),
                _sqlite_index_where(index),
            )
            for index in table.indexes
            if not (
                pre_reliability
                and table_name == "evaluation_runs"
                and str(index.name) in _RELIABILITY_INDEX_NAMES
            )
            and not (
                pre_governance
                and table_name == "evaluation_runs"
                and str(index.name) in _GOVERNANCE_EXISTING_INDEX_NAMES
            )
            and not (
                pre_worker_progress
                and table_name == "audit_events"
                and str(index.name) in _WORKER_PROGRESS_EXISTING_INDEX_NAMES
            )
        }
        actual_indexes = {
            (
                str(index["name"]),
                bool(index["unique"]),
                tuple(index["column_names"]),
                _normalized_check_sql(index.get("dialect_options", {}).get("sqlite_where", "")),
            )
            for index in inspector.get_indexes(table_name)
        }

        if (
            actual_declaration_counts != expected_declaration_counts
            or actual_primary_key != expected_primary_key
            or actual_uniques != expected_uniques
            or actual_foreign_keys != expected_foreign_keys
            or actual_indexes != expected_indexes
        ):
            raise SchemaPreparationError(
                f"Keys or indexes on table {table_name} do not match the supported schema; "
                "no migration marker was written."
            )


def _validate_check_constraints(connection: Connection, *, schema_revision: str) -> None:
    inspector = sa.inspect(connection)
    legacy = schema_revision == LEGACY_REVISION
    pre_reliability = schema_revision in {LEGACY_REVISION, PHASE_1_REVISION}
    pre_credentials = schema_revision in {
        LEGACY_REVISION,
        PHASE_1_REVISION,
        RELIABILITY_REVISION,
    }
    pre_governance = schema_revision in {
        LEGACY_REVISION,
        PHASE_1_REVISION,
        RELIABILITY_REVISION,
        CREDENTIAL_REVISION,
    }
    for table_name, table in _schema_tables_for_revision(schema_revision).items():
        expected_entries = [
            (str(constraint.name), _normalized_check_sql(constraint.sqltext))
            for constraint in table.constraints
            if isinstance(constraint, sa.CheckConstraint)
        ]
        if legacy and table_name == "models":
            expected_entries = [
                entry
                for entry in expected_entries
                if entry[0]
                not in {
                    "ck_models_credential_source_values",
                    "ck_models_mock_configuration_empty",
                    "ck_models_openai_configuration_required",
                }
            ]
        elif pre_credentials and table_name == "models":
            expected_entries = [
                entry
                for entry in expected_entries
                if entry[0]
                not in {
                    "ck_models_credential_source_values",
                    "ck_models_mock_configuration_empty",
                    "ck_models_openai_configuration_required",
                }
            ]
            expected_entries.extend(
                [
                    (
                        "ck_models_mock_configuration_empty",
                        "provider_type != 'mock' OR (base_url IS NULL AND "
                        "remote_model_name IS NULL AND api_key_env IS NULL)",
                    ),
                    (
                        "ck_models_openai_configuration_required",
                        "provider_type != 'openai_compatible' OR (base_url IS NOT NULL "
                        "AND remote_model_name IS NOT NULL AND api_key_env IS NOT NULL)",
                    ),
                ]
            )
        if pre_governance and table_name in _GOVERNANCE_CHECK_NAMES_BY_TABLE:
            expected_entries = [
                entry
                for entry in expected_entries
                if entry[0] not in _GOVERNANCE_CHECK_NAMES_BY_TABLE[table_name]
            ]
            if table_name == "evaluation_runs":
                expected_entries.append(
                    (
                        "ck_evaluation_runs_attempt_within_limit",
                        "attempt_count <= max_attempts",
                    )
                )
        if pre_reliability and table_name == "evaluation_runs":
            expected_entries = [
                entry for entry in expected_entries if entry[0] not in _RELIABILITY_CHECK_NAMES
            ]
        expected = tuple(sorted(expected_entries))
        table_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).scalar_one()
        declared_check_count = _sqlite_sql_tokens(table_sql).count("CHECK")
        actual = tuple(
            sorted(
                (str(constraint["name"]), _normalized_check_sql(constraint["sqltext"]))
                for constraint in inspector.get_check_constraints(table_name)
            )
        )
        if actual != expected or declared_check_count != len(expected):
            raise SchemaPreparationError(
                f"Check constraints on table {table_name} do not match the supported schema; "
                "no migration marker was written."
            )


def _validate_sqlite_database(connection: Connection, *, schema_revision: str) -> None:
    _validate_sqlite_ddl_modifiers(connection, schema_revision=schema_revision)
    _validate_sqlite_table_options(connection, schema_revision=schema_revision)
    _validate_relational_structure(connection, schema_revision=schema_revision)
    _validate_check_constraints(connection, schema_revision=schema_revision)
    triggers = connection.exec_driver_sql(
        "SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'"
    ).all()
    domain_tables = set(Base.metadata.tables)
    if any(table_name in domain_tables for _trigger_name, table_name in triggers):
        raise SchemaPreparationError(
            "SQLite triggers on LLMBenchLab tables are not part of a supported schema; "
            "no migration marker was written."
        )
    integrity_results = connection.exec_driver_sql("PRAGMA integrity_check").scalars().all()
    if integrity_results != ["ok"]:
        raise SchemaPreparationError(
            "SQLite integrity_check failed; no migration marker was written. "
            "Restore or repair the database before retrying."
        )
    foreign_key_violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if foreign_key_violations:
        raise SchemaPreparationError(
            "SQLite foreign_key_check found violations; no migration marker was written."
        )
    if schema_revision != LEGACY_REVISION:
        return
    invalid_models = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM models WHERE "
            "(provider_type = 'openai_compatible' AND "
            "(base_url IS NULL OR remote_model_name IS NULL OR api_key_env IS NULL)) OR "
            "(provider_type = 'mock' AND "
            "(base_url IS NOT NULL OR remote_model_name IS NOT NULL OR api_key_env IS NOT NULL))"
        )
    ).scalar_one()
    if invalid_models:
        raise SchemaPreparationError(
            "Legacy model rows do not satisfy the current provider configuration rules; "
            "no migration marker was written."
        )


def validate_sqlite_schema_fingerprint(
    connection: Connection,
    *,
    schema_revision: str,
) -> None:
    """Validate a read-only SQLite schema against one supported revision."""

    _validate_sqlite_database(connection, schema_revision=schema_revision)


def _sqlite_database_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise SchemaPreparationError(
            "Automatic adoption of unversioned application tables is supported only for SQLite."
        )
    if not url.database or url.database == ":memory:":
        raise SchemaPreparationError(
            "An in-memory SQLite database with existing unversioned tables cannot be adopted."
        )
    return Path(url.database).expanduser().resolve()


def _backup_sqlite_database(database_url: str) -> Path:
    source_path = _sqlite_database_path(database_url)
    if not source_path.is_file():
        raise SchemaPreparationError(f"SQLite database file does not exist: {source_path}")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = source_path.with_name(f"{source_path.name}.pre-alembic-{timestamp}.bak")
    with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as destination:
        source.backup(destination)
    os.chmod(backup_path, source_path.stat().st_mode & 0o777)
    return backup_path


def prepare_database(
    database_url: str,
    *,
    config_path: Path = ALEMBIC_CONFIG_PATH,
) -> PreparationResult:
    """Validate, back up, and stamp a supported unversioned application database."""

    engine = create_database_engine(database_url)
    connection = engine.connect()
    sqlite_locked = connection.dialect.name == "sqlite"
    try:
        if sqlite_locked:
            # Keep a reserved write lock from validation through backup and stamp so a
            # running development process cannot change the source between those steps.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        current_heads = database_heads(connection)
        table_names = set(sa.inspect(connection).get_table_names())
        domain_tables = set(Base.metadata.tables)
        present_domain_tables = table_names & domain_tables

        expected_heads = expected_database_heads(config_path)
        head_revision = expected_heads[0]
        historical_revisions = (
            LEGACY_REVISION,
            PHASE_1_REVISION,
            RELIABILITY_REVISION,
            CREDENTIAL_REVISION,
            GOVERNANCE_REVISION,
        )
        historical_heads = {(revision,) for revision in historical_revisions}

        if current_heads and current_heads not in historical_heads:
            if set(current_heads) == set(expected_heads):
                if present_domain_tables != domain_tables:
                    missing = ", ".join(sorted(domain_tables - present_domain_tables))
                    raise SchemaPreparationError(
                        "The versioned database at head contains only part of the "
                        f"LLMBenchLab schema (missing: {missing}); startup is unsafe."
                    )
                differences = _schema_differences(connection)
                if differences:
                    rendered_differences = ", ".join(sorted(differences))
                    raise SchemaPreparationError(
                        "The versioned database at head does not match the application "
                        f"schema. Differences: {rendered_differences}"
                    )
                if sqlite_locked:
                    _validate_sqlite_database(connection, schema_revision=head_revision)
            return PreparationResult(action="versioned")
        if not present_domain_tables:
            return PreparationResult(action="empty")
        if current_heads in historical_heads:
            expected_present_tables = set(_schema_tables_for_revision(current_heads[0]))
            if present_domain_tables != expected_present_tables:
                missing = ", ".join(sorted(expected_present_tables - present_domain_tables))
                raise SchemaPreparationError(
                    "Database contains only part of the LLMBenchLab schema "
                    f"(missing: {missing}); no migration marker was written."
                )
        elif not current_heads:
            supported_table_sets = {
                frozenset(_schema_tables_for_revision(revision))
                for revision in (*historical_revisions, head_revision)
            }
            if frozenset(present_domain_tables) not in supported_table_sets:
                missing = ", ".join(sorted(domain_tables - present_domain_tables))
                raise SchemaPreparationError(
                    "Database contains only part of the LLMBenchLab schema "
                    f"(missing: {missing}); no migration marker was written."
                )

        target_revision: str | None
        source_revision: str
        if current_heads in historical_heads:
            if not sqlite_locked:
                return PreparationResult(action="versioned")
            source_revision = current_heads[0]
            if source_revision == LEGACY_REVISION:
                expected_differences = _LEGACY_DIFFERENCES
            elif source_revision == PHASE_1_REVISION:
                expected_differences = _PHASE_1_DIFFERENCES
            elif source_revision == RELIABILITY_REVISION:
                expected_differences = _RELIABILITY_DIFFERENCES
            else:
                expected_differences = (
                    _CREDENTIAL_DIFFERENCES
                    if source_revision == CREDENTIAL_REVISION
                    else _WORKER_PROGRESS_DIFFERENCES
                )
            differences = _schema_differences(connection)
            if differences != expected_differences:
                rendered_differences = ", ".join(sorted(differences)) or "unknown"
                raise SchemaPreparationError(
                    "The versioned historical database does not match its expected schema; "
                    f"upgrade was not started. Differences: {rendered_differences}"
                )
            _validate_sqlite_database(connection, schema_revision=source_revision)
            target_revision = None
            action = {
                LEGACY_REVISION: "versioned_legacy",
                PHASE_1_REVISION: "versioned_phase1",
                RELIABILITY_REVISION: "versioned_reliability",
                CREDENTIAL_REVISION: "versioned_credentials",
                GOVERNANCE_REVISION: "versioned_governance",
            }[source_revision]
        else:
            if not sqlite_locked:
                raise SchemaPreparationError(
                    "Automatic adoption of unversioned application tables is supported only "
                    "for SQLite."
                )
            differences = _schema_differences(connection)
            if not differences:
                target_revision = "head"
                action = "stamped_current"
                source_revision = head_revision
            elif differences == _PHASE_1_DIFFERENCES:
                target_revision = PHASE_1_REVISION
                action = "stamped_phase1"
                source_revision = PHASE_1_REVISION
            elif differences == _RELIABILITY_DIFFERENCES:
                target_revision = RELIABILITY_REVISION
                action = "stamped_reliability"
                source_revision = RELIABILITY_REVISION
            elif differences == _CREDENTIAL_DIFFERENCES:
                target_revision = CREDENTIAL_REVISION
                action = "stamped_credentials"
                source_revision = CREDENTIAL_REVISION
            elif differences == _WORKER_PROGRESS_DIFFERENCES:
                target_revision = GOVERNANCE_REVISION
                action = "stamped_governance"
                source_revision = GOVERNANCE_REVISION
            elif differences == _LEGACY_DIFFERENCES:
                target_revision = LEGACY_REVISION
                action = "stamped_legacy"
                source_revision = LEGACY_REVISION
            else:
                rendered_differences = ", ".join(sorted(differences)) or "unknown"
                raise SchemaPreparationError(
                    "Database has unversioned LLMBenchLab tables but does not match a supported "
                    "schema; no migration marker was written. Differences: "
                    f"{rendered_differences}"
                )
            _validate_sqlite_database(connection, schema_revision=source_revision)

        backup_path = _backup_sqlite_database(database_url)
        if target_revision is not None:
            _stamp_connection(connection, target_revision, config_path=config_path)
            connection.commit()
        elif connection.in_transaction():
            connection.rollback()

        return PreparationResult(
            action=action,
            backup_path=backup_path,
            stamped_revision=source_revision,
        )
    except Exception:
        if connection.in_transaction():
            connection.rollback()
        raise
    finally:
        connection.close()
        engine.dispose()


def main() -> int:
    """CLI used by setup, migrate, and the container entrypoint."""

    try:
        result = prepare_database(get_settings().database_url)
    except SchemaPreparationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if result.action == "stamped_legacy":
        print(
            "Adopted a verified legacy SQLite schema at "
            f"{result.stamped_revision}; backup: {result.backup_path}"
        )
    elif result.action == "stamped_phase1":
        print(
            "Adopted a verified Phase 1 SQLite schema at "
            f"{result.stamped_revision}; backup: {result.backup_path}"
        )
    elif result.action == "stamped_reliability":
        print(
            "Adopted a verified reliability SQLite schema at "
            f"{result.stamped_revision}; backup: {result.backup_path}"
        )
    elif result.action == "stamped_credentials":
        print(
            "Adopted a verified Web-credential SQLite schema at "
            f"{result.stamped_revision}; backup: {result.backup_path}"
        )
    elif result.action == "stamped_current":
        print(
            "Adopted a verified current SQLite schema at "
            f"{result.stamped_revision}; backup: {result.backup_path}"
        )
    elif result.action == "stamped_governance":
        print(
            "Adopted a verified governance/audit SQLite schema at "
            f"{result.stamped_revision}; backup: {result.backup_path}"
        )
    elif result.action == "versioned_legacy":
        print(
            "Verified a versioned legacy SQLite schema before upgrade; "
            f"backup: {result.backup_path}"
        )
    elif result.action == "versioned_phase1":
        print(
            "Verified a versioned Phase 1 SQLite schema before upgrade; "
            f"backup: {result.backup_path}"
        )
    elif result.action == "versioned_reliability":
        print(
            "Verified a versioned reliability SQLite schema before upgrade; "
            f"backup: {result.backup_path}"
        )
    elif result.action == "versioned_credentials":
        print(
            "Verified a versioned Web-credential SQLite schema before upgrade; "
            f"backup: {result.backup_path}"
        )
    elif result.action == "versioned_governance":
        print(
            "Verified a versioned governance/audit SQLite schema before upgrade; "
            f"backup: {result.backup_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
