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

_LEGACY_DIFFERENCES = tuple(
    sorted(
        {
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


def _validate_sqlite_ddl_modifiers(connection: Connection) -> None:
    """Reject SQLite syntax whose semantics are omitted by reflection/autogenerate."""

    domain_tables = set(Base.metadata.tables)
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
            has_unsupported_modifier = has_unsupported_modifier or "WHERE" in tokens
        if has_unsupported_modifier:
            raise SchemaPreparationError(
                "SQLite DDL modifiers on "
                f"{schema_object['type']} {schema_object['name']} do not match the supported "
                "schema; conflict policies, collations, generated columns, deferred foreign "
                "keys, ordered/partial indexes, and related modifiers cannot be adopted."
            )


def _validate_sqlite_table_options(connection: Connection) -> None:
    table_options = {
        row["name"]: row
        for row in connection.exec_driver_sql("PRAGMA table_list").mappings()
        if row["schema"] == "main" and row["name"] in Base.metadata.tables
    }
    for table_name in Base.metadata.tables:
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


def _validate_relational_structure(connection: Connection, *, legacy: bool) -> None:
    inspector = sa.inspect(connection)
    for table_name, table in Base.metadata.tables.items():
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
        expected_declaration_counts = (
            int(bool(table.primary_key.columns)),
            expected_unique_count,
            len(table.foreign_key_constraints),
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
                "",
            )
            for index in table.indexes
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


def _validate_check_constraints(connection: Connection, *, legacy: bool) -> None:
    inspector = sa.inspect(connection)
    for table_name, table in Base.metadata.tables.items():
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
                    "ck_models_mock_configuration_empty",
                    "ck_models_openai_configuration_required",
                }
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


def _validate_sqlite_database(connection: Connection, *, legacy: bool) -> None:
    _validate_sqlite_ddl_modifiers(connection)
    _validate_sqlite_table_options(connection)
    _validate_relational_structure(connection, legacy=legacy)
    _validate_check_constraints(connection, legacy=legacy)
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
    if not legacy:
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

        if current_heads and current_heads != (LEGACY_REVISION,):
            if set(current_heads) == set(expected_database_heads(config_path)):
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
                    _validate_sqlite_database(connection, legacy=False)
            return PreparationResult(action="versioned")
        if not present_domain_tables:
            return PreparationResult(action="empty")
        if present_domain_tables != domain_tables:
            missing = ", ".join(sorted(domain_tables - present_domain_tables))
            raise SchemaPreparationError(
                "Database contains only part of the LLMBenchLab schema "
                f"(missing: {missing}); no migration marker was written."
            )

        target_revision: str | None
        if current_heads == (LEGACY_REVISION,):
            if not sqlite_locked:
                raise SchemaPreparationError(
                    "Automatic legacy schema preparation is supported only for SQLite."
                )
            differences = _schema_differences(connection)
            if differences != _LEGACY_DIFFERENCES:
                rendered_differences = ", ".join(sorted(differences)) or "unknown"
                raise SchemaPreparationError(
                    "The versioned legacy database does not match its expected schema; "
                    f"upgrade was not started. Differences: {rendered_differences}"
                )
            _validate_sqlite_database(connection, legacy=True)
            target_revision = None
            action = "versioned_legacy"
        else:
            differences = _schema_differences(connection)
            if not differences:
                target_revision = "head"
                action = "stamped_current"
                legacy = False
            elif differences == _LEGACY_DIFFERENCES:
                target_revision = LEGACY_REVISION
                action = "stamped_legacy"
                legacy = True
            else:
                rendered_differences = ", ".join(sorted(differences)) or "unknown"
                raise SchemaPreparationError(
                    "Database has unversioned LLMBenchLab tables but does not match a supported "
                    "schema; no migration marker was written. Differences: "
                    f"{rendered_differences}"
                )
            if sqlite_locked:
                _validate_sqlite_database(connection, legacy=legacy)
            elif legacy:
                raise SchemaPreparationError(
                    "Automatic legacy schema adoption is supported only for SQLite."
                )

        backup_path = _backup_sqlite_database(database_url)
        if target_revision is not None:
            _stamp_connection(connection, target_revision, config_path=config_path)
            connection.commit()
        elif connection.in_transaction():
            connection.rollback()

        if target_revision == LEGACY_REVISION or action == "versioned_legacy":
            stamped_revision = LEGACY_REVISION
        else:
            stamped_revision = expected_database_heads(config_path)[0]
        return PreparationResult(
            action=action,
            backup_path=backup_path,
            stamped_revision=stamped_revision,
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
    elif result.action == "stamped_current":
        print(
            "Adopted a verified current SQLite schema at "
            f"{result.stamped_revision}; backup: {result.backup_path}"
        )
    elif result.action == "versioned_legacy":
        print(
            "Verified a versioned legacy SQLite schema before upgrade; "
            f"backup: {result.backup_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
