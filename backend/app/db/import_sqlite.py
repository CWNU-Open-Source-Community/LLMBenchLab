"""Explicit, one-way import from a stopped SQLite database into PostgreSQL."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import Table

from app import models as _models  # noqa: F401 -- registers all mapped tables
from app.db.base import Base
from app.db.prepare_migrations import database_heads, expected_database_heads
from app.db.session import create_database_engine

CORE_TABLE_NAMES = (
    "models",
    "model_credentials",
    "benchmarks",
    "questions",
    "evaluation_runs",
    "evaluation_responses",
)
DEFAULT_TARGET_ENV = "LLMBENCHLAB_DATABASE_URL"


class SQLiteImportError(RuntimeError):
    """Raised when an import precondition or verification check fails."""


class SQLiteImportCommittedVerificationError(SQLiteImportError):
    """Raised when target rows committed but post-commit verification did not finish."""


class SQLiteImportCommitOutcomeUnknownError(SQLiteImportError):
    """Raised when PostgreSQL did not confirm whether COMMIT reached the server."""


@dataclass(frozen=True)
class TableSummary:
    """Content-free reconciliation evidence for one table."""

    row_count: int
    pk_set_digest: str
    canonical_row_digest: str


@dataclass(frozen=True)
class DatabaseSnapshot:
    """Typed rows and their deterministic summaries from one database snapshot."""

    rows: Mapping[str, tuple[Mapping[str, Any], ...]]
    summaries: Mapping[str, TableSummary]


@dataclass(frozen=True)
class ImportReport:
    """Source, pre-commit target, and committed target reconciliation evidence."""

    source: Mapping[str, TableSummary]
    precommit_target: Mapping[str, TableSummary]
    postcommit_target: Mapping[str, TableSummary]


def _canonical_value(value: Any) -> Any:
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, Enum):
        return ["enum", str(value.value)]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, int):
        return ["integer", str(value)]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SQLiteImportError("Canonical summaries reject non-finite Decimal values")
        normalized = "0" if value == 0 else format(value.normalize(), "f")
        return ["decimal", normalized]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SQLiteImportError("Canonical summaries reject non-finite float values")
        normalized = 0.0 if value == 0 else value
        return ["float", normalized.hex()]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        normalized = value.astimezone(UTC).isoformat(timespec="microseconds")
        return ["datetime", normalized.replace("+00:00", "Z")]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise SQLiteImportError("Canonical JSON objects require string keys")
        return [
            "object",
            [[key, _canonical_value(value[key])] for key in sorted(value)],
        ]
    if isinstance(value, (list, tuple)):
        return ["array", [_canonical_value(item) for item in value]]
    raise SQLiteImportError(
        f"Canonical summaries do not support values of type {type(value).__name__}"
    )


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_payloads(payloads: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for payload in sorted(payloads):
        digest.update(len(payload).to_bytes(8, byteorder="big"))
        digest.update(payload)
    return digest.hexdigest()


def canonical_table_summary(
    table: Table,
    rows: Iterable[Mapping[str, Any]],
) -> TableSummary:
    """Return an order-independent, cross-dialect summary of typed table rows."""

    materialized = tuple(rows)
    primary_key_columns = tuple(table.primary_key.columns)
    if not primary_key_columns:
        raise SQLiteImportError(f"Table {table.name} has no primary key")

    row_payloads: list[bytes] = []
    primary_key_payloads: list[bytes] = []
    for row in materialized:
        try:
            row_values = tuple((column.name, row[column.name]) for column in table.columns)
            primary_key_values = tuple(
                (column.name, row[column.name]) for column in primary_key_columns
            )
        except KeyError as exc:
            raise SQLiteImportError(
                f"Table {table.name} summary is missing column {exc.args[0]}"
            ) from exc
        row_payloads.append(_canonical_json_bytes(row_values))
        primary_key_payloads.append(_canonical_json_bytes(primary_key_values))

    return TableSummary(
        row_count=len(materialized),
        pk_set_digest=_digest_payloads(primary_key_payloads),
        canonical_row_digest=_digest_payloads(row_payloads),
    )


def snapshot_database(connection: Connection) -> DatabaseSnapshot:
    """Read every core table through shared SQLAlchemy types in dependency order."""

    rows_by_table: dict[str, tuple[Mapping[str, Any], ...]] = {}
    summaries: dict[str, TableSummary] = {}
    for table_name in CORE_TABLE_NAMES:
        table = Base.metadata.tables[table_name]
        statement = sa.select(table).order_by(*table.primary_key.columns)
        rows = tuple(dict(row) for row in connection.execute(statement).mappings())
        rows_by_table[table_name] = rows
        summaries[table_name] = canonical_table_summary(table, rows)
    return DatabaseSnapshot(rows=rows_by_table, summaries=summaries)


def copy_snapshot(snapshot: DatabaseSnapshot, target: Connection) -> None:
    """Copy a snapshot using the caller's transaction and dependency ordering."""

    for table_name in CORE_TABLE_NAMES:
        table = Base.metadata.tables[table_name]
        rows = snapshot.rows.get(table_name)
        if rows is None:
            raise SQLiteImportError(f"Source snapshot is missing table {table_name}")
        if rows:
            target.execute(table.insert(), [dict(row) for row in rows])


def _require_database_head(connection: Connection, *, role: str) -> None:
    expected = set(expected_database_heads())
    current = set(database_heads(connection))
    if current == expected:
        return
    current_label = ",".join(sorted(current)) if current else "unversioned"
    expected_label = ",".join(sorted(expected))
    raise SQLiteImportError(
        f"{role} database must be at Alembic head "
        f"(current={current_label}, expected={expected_label})"
    )


def preflight_sqlite_source(connection: Connection) -> None:
    """Validate a read-only, stopped SQLite source before reading application data."""

    if connection.dialect.name != "sqlite":
        raise SQLiteImportError("Source database must use SQLite")
    if connection.exec_driver_sql("PRAGMA query_only").scalar_one() != 1:
        raise SQLiteImportError("Source SQLite connection is not read-only")

    integrity_results = [
        str(row[0]) for row in connection.exec_driver_sql("PRAGMA integrity_check").all()
    ]
    if integrity_results != ["ok"]:
        raise SQLiteImportError(
            f"Source SQLite integrity_check failed ({len(integrity_results)} result rows)"
        )
    foreign_key_violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if foreign_key_violations:
        raise SQLiteImportError(
            f"Source SQLite foreign_key_check failed ({len(foreign_key_violations)} violation rows)"
        )

    _require_database_head(connection, role="Source SQLite")
    active_runs = connection.execute(
        sa.select(sa.func.count())
        .select_from(Base.metadata.tables["evaluation_runs"])
        .where(Base.metadata.tables["evaluation_runs"].c.status.in_(("pending", "running")))
    ).scalar_one()
    if active_runs:
        raise SQLiteImportError(
            "Source SQLite contains pending or running evaluation runs; stop creation and "
            "drain, cancel, or fail active runs before importing"
        )


def _require_empty_target(connection: Connection) -> None:
    nonempty: list[str] = []
    for table_name in CORE_TABLE_NAMES:
        table = Base.metadata.tables[table_name]
        row_count = connection.scalar(sa.select(sa.func.count()).select_from(table))
        if row_count:
            nonempty.append(f"{table_name}={row_count}")
    if nonempty:
        raise SQLiteImportError(
            "Target PostgreSQL core tables must be empty (" + ", ".join(nonempty) + ")"
        )


def _lock_target_tables(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    table_names = ", ".join(("alembic_version", *CORE_TABLE_NAMES))
    connection.exec_driver_sql(f"LOCK TABLE {table_names} IN ACCESS EXCLUSIVE MODE")


def _serialize_target_import(connection: Connection) -> None:
    """Serialize import preflight before either contender reads or locks target tables."""

    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(1280068930, 1230196048)")


def _assert_summaries_match(
    expected: Mapping[str, TableSummary],
    actual: Mapping[str, TableSummary],
    *,
    phase: str,
) -> None:
    mismatched = [
        table_name
        for table_name in CORE_TABLE_NAMES
        if expected.get(table_name) != actual.get(table_name)
    ]
    if mismatched:
        raise SQLiteImportError(
            f"SQLite to PostgreSQL reconciliation failed during {phase} for tables: "
            + ", ".join(mismatched)
        )


def _emit_summaries(
    summaries: Mapping[str, TableSummary],
    *,
    phase: str,
    output: TextIO,
) -> None:
    for table_name in CORE_TABLE_NAMES:
        summary = summaries[table_name]
        print(
            "import_summary "
            f"phase={phase} table={table_name} row_count={summary.row_count} "
            f"pk_set_digest=sha256:{summary.pk_set_digest} "
            f"canonical_row_digest=sha256:{summary.canonical_row_digest}",
            file=output,
        )


def _sqlite_path(source: str) -> Path:
    if source.startswith("sqlite:") or "://" in source:
        try:
            source_url = make_url(source)
        except ArgumentError as exc:
            raise SQLiteImportError("Source must be a valid SQLite URL or filesystem path") from exc
        if source_url.get_backend_name() != "sqlite":
            raise SQLiteImportError("Source database must use SQLite")
        if not source_url.database or source_url.database == ":memory:":
            raise SQLiteImportError("Source SQLite database must be a filesystem file")
        source_path = Path(source_url.database)
    else:
        source_path = Path(source)

    resolved = source_path.expanduser().resolve()
    if not resolved.is_file():
        raise SQLiteImportError("Source SQLite database file does not exist")
    return resolved


def _postgresql_url(target: str) -> str:
    try:
        target_url = make_url(target)
    except ArgumentError as exc:
        raise SQLiteImportError("Target must be a valid PostgreSQL URL") from exc
    if target_url.get_backend_name() != "postgresql":
        raise SQLiteImportError("Target database must use PostgreSQL")
    return target


def _read_only_sqlite_engine(source_path: Path) -> Engine:
    source_uri = f"{source_path.as_uri()}?mode=ro"

    def connect_read_only() -> sqlite3.Connection:
        connection = sqlite3.connect(
            source_uri,
            uri=True,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only=ON")
        return connection

    return sa.create_engine(
        "sqlite+pysqlite://",
        creator=connect_read_only,
        poolclass=NullPool,
    )


def import_sqlite_to_postgres(
    source: str,
    target: str,
    *,
    output: TextIO | None = None,
) -> ImportReport:
    """Validate and atomically copy one stopped SQLite database into empty PostgreSQL."""

    destination = output if output is not None else sys.stdout
    source_path = _sqlite_path(source)
    target_url = _postgresql_url(target)
    source_engine = _read_only_sqlite_engine(source_path)
    target_engine: Engine | None = None
    try:
        target_engine = create_database_engine(target_url)
        with source_engine.connect() as source_connection:
            source_connection.exec_driver_sql("BEGIN")
            preflight_sqlite_source(source_connection)
            source_snapshot = snapshot_database(source_connection)
            _emit_summaries(source_snapshot.summaries, phase="source", output=destination)

        commit_confirmed = False
        try:
            with target_engine.connect() as target_connection:
                target_transaction = target_connection.begin()
                try:
                    _serialize_target_import(target_connection)
                    _require_database_head(target_connection, role="Target PostgreSQL")
                    _lock_target_tables(target_connection)
                    _require_database_head(target_connection, role="Target PostgreSQL")
                    _require_empty_target(target_connection)
                    copy_snapshot(source_snapshot, target_connection)
                    precommit_snapshot = snapshot_database(target_connection)
                    _assert_summaries_match(
                        source_snapshot.summaries,
                        precommit_snapshot.summaries,
                        phase="precommit",
                    )
                    _emit_summaries(
                        precommit_snapshot.summaries,
                        phase="precommit_target",
                        output=destination,
                    )
                except Exception:
                    target_transaction.rollback()
                    raise
                try:
                    target_transaction.commit()
                except Exception as exc:
                    raise SQLiteImportCommitOutcomeUnknownError(
                        "PostgreSQL did not confirm COMMIT; the target may be empty or may "
                        "already contain the complete imported rows; do not retry blindly"
                    ) from exc
                commit_confirmed = True
        except SQLiteImportCommitOutcomeUnknownError:
            raise
        except Exception as exc:
            if commit_confirmed:
                raise SQLiteImportCommittedVerificationError(
                    "Target transaction committed, but connection cleanup did not finish; "
                    "do not retry blindly because the target tables may already contain the "
                    "imported rows"
                ) from exc
            raise

        try:
            with (
                target_engine.connect().execution_options(
                    isolation_level="REPEATABLE READ"
                ) as target_connection,
                target_connection.begin(),
            ):
                target_connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                _require_database_head(target_connection, role="Target PostgreSQL")
                postcommit_snapshot = snapshot_database(target_connection)
            _assert_summaries_match(
                source_snapshot.summaries,
                postcommit_snapshot.summaries,
                phase="postcommit",
            )
            _emit_summaries(
                postcommit_snapshot.summaries,
                phase="postcommit_target",
                output=destination,
            )
        except Exception as exc:
            raise SQLiteImportCommittedVerificationError(
                "Target transaction committed, but post-commit verification or reporting "
                "did not finish; do not retry blindly because the target tables may already "
                "contain the imported rows"
            ) from exc
        return ImportReport(
            source=source_snapshot.summaries,
            precommit_target=precommit_snapshot.summaries,
            postcommit_target=postcommit_snapshot.summaries,
        )
    finally:
        # Pool disposal cannot change the database outcome already reported above.
        # Do not let a best-effort connection close mask rollback, commit-unknown,
        # committed-verification-failed, or success semantics.
        for engine in (source_engine, target_engine):
            if engine is not None:
                with suppress(Exception):
                    engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a stopped LLMBenchLab SQLite database into empty PostgreSQL",
    )
    parser.add_argument("--source", required=True, help="SQLite URL or filesystem path")
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--target",
        help="Passwordless PostgreSQL URL; use --target-env for a credentialed URL",
    )
    target.add_argument(
        "--target-env",
        metavar="ENV_VAR",
        help=(
            "Read the PostgreSQL URL from this environment variable instead of argv "
            f"(default: {DEFAULT_TARGET_ENV})"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the explicit SQLite-to-PostgreSQL importer CLI."""

    arguments = _parser().parse_args(argv)
    try:
        target = arguments.target
        if target is not None:
            try:
                target_url = make_url(target)
            except ArgumentError as exc:
                raise SQLiteImportError("Target must be a valid PostgreSQL URL") from exc
            password_in_query = any(
                str(key).lower().endswith("password") for key in target_url.query
            )
            if target_url.password is not None or password_in_query:
                raise SQLiteImportError(
                    "--target must not contain a password; use --target-env, PGPASSFILE, "
                    "or a libpq service instead"
                )
        else:
            target_env = arguments.target_env or DEFAULT_TARGET_ENV
            target = os.environ.get(target_env)
            if not target:
                raise SQLiteImportError(f"Target environment variable {target_env} is not set")
        import_sqlite_to_postgres(arguments.source, target)
    except SQLiteImportCommitOutcomeUnknownError:
        print(
            "SQLite to PostgreSQL import status=commit_outcome_unknown; PostgreSQL did "
            "not confirm COMMIT, so the target may be empty or fully populated; do not "
            "retry blindly; inspect the target and reconciliation evidence",
            file=sys.stderr,
        )
        return 4
    except SQLiteImportCommittedVerificationError:
        print(
            "SQLite to PostgreSQL import status=committed_but_verification_failed; "
            "the target transaction committed, so do not retry blindly; inspect the "
            "target and reconciliation evidence",
            file=sys.stderr,
        )
        return 3
    except SQLiteImportError as exc:
        print(f"SQLite to PostgreSQL import failed: {exc}", file=sys.stderr)
        return 2
    except (SQLAlchemyError, sqlite3.Error, OSError) as exc:
        print(
            "SQLite to PostgreSQL import failed with a database or filesystem error "
            f"({type(exc).__name__}); no row contents or connection URLs are shown",
            file=sys.stderr,
        )
        return 2
    print("SQLite to PostgreSQL import completed and reconciled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
