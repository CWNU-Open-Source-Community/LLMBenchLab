"""Regression tests for clean migrations and safe legacy SQLite adoption."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import app.db.prepare_migrations as prepare_migrations_module
from app.db.base import Base
from app.db.prepare_migrations import (
    CREDENTIAL_REVISION,
    GOVERNANCE_REVISION,
    INDEX_REPAIR_REVISION,
    LEGACY_REVISION,
    PHASE_1_REVISION,
    WORKER_PROGRESS_REVISION,
    SchemaPreparationError,
    database_heads,
    prepare_database,
    require_database_at_head,
    stamp_database,
)
from app.db.session import create_database_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
HEAD_REVISION = "20260830_0007"
WEB_CREDENTIAL_REVISION = CREDENTIAL_REVISION
RELIABILITY_REVISION = "20260825_0002"


def _database_url(database_path: Path) -> str:
    return f"sqlite:///{database_path}"


def _invoke_alembic(database_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["LLMBENCHLAB_DATABASE_URL"] = _database_url(database_path)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_alembic(database_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = _invoke_alembic(database_path, *arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _read_heads(database_path: Path) -> tuple[str, ...]:
    engine = create_database_engine(_database_url(database_path))
    try:
        with engine.connect() as connection:
            return database_heads(connection)
    finally:
        engine.dispose()


def _insert_legacy_rows(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            INSERT INTO models (
                id, name, provider_type, base_url, remote_model_name, api_key_env,
                enabled, input_price_per_million, output_price_per_million,
                default_parameters, created_at, updated_at
            ) VALUES (
                'model-1', 'Legacy Mock', 'mock', NULL, NULL, NULL,
                1, 0, 0, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );

            INSERT INTO benchmarks (
                id, slug, name, version, description, dimension, language, license,
                source, evaluator_type, evaluator_config, prompt_template,
                schema_version, dataset_hash, question_count, is_demo, created_at
            ) VALUES (
                'benchmark-1', 'legacy-demo', 'Legacy Demo', '1.0.0', 'fixture',
                'general', 'en', 'MIT', 'local', 'exact_match', '{}', '{}',
                'llmbenchlab-dataset-v1', 'legacy-hash', 2, 1, CURRENT_TIMESTAMP
            );

            INSERT INTO questions (
                id, benchmark_id, external_id, question_type, prompt, choices,
                reference_answer, evaluator_config, metadata
            ) VALUES (
                'question-1', 'benchmark-1', 'z-first-inserted', 'exact_match',
                'First?', NULL, '"first"', '{}', '{}'
            );
            INSERT INTO questions (
                id, benchmark_id, external_id, question_type, prompt, choices,
                reference_answer, evaluator_config, metadata
            ) VALUES (
                'question-2', 'benchmark-1', 'a-second-inserted', 'exact_match',
                'Second?', NULL, '"second"', '{}', '{}'
            );

            INSERT INTO evaluation_runs (
                id, model_id, benchmark_id, status, protocol_version,
                model_parameters_snapshot, benchmark_hash_snapshot,
                prompt_template_snapshot, total_questions, completed_questions,
                correct_questions, error_questions, cancellation_requested, created_at
            ) VALUES (
                'run-1', 'model-1', 'benchmark-1', 'completed',
                'llmbenchlab-protocol-v1', '{}', 'legacy-hash', '{}',
                2, 1, 1, 0, 0, CURRENT_TIMESTAMP
            );

            INSERT INTO evaluation_responses (
                id, run_id, question_id, reference_answer_snapshot, score,
                evaluator_name, created_at
            ) VALUES (
                'response-1', 'run-1', 'question-1', '"first"', 1,
                'exact_match', CURRENT_TIMESTAMP
            );
            """
        )


def _drop_known_governance_indexes(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        for index_name in (
            "ix_evaluation_runs_started_at_id",
            "ix_evaluation_runs_finished_at_id",
            "uq_governance_policies_single_active",
        ):
            connection.execute(f'DROP INDEX "{index_name}"')


def _insert_active_policy(
    connection: sqlite3.Connection,
    *,
    policy_id: str,
    version: int,
) -> None:
    connection.execute(
        "INSERT INTO governance_policies ("
        "id, version, policy_hash, is_active, backlog_limit, question_quantum, "
        "activated_at, created_at"
        ") VALUES (?, ?, ?, 1, 100, 25, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (policy_id, version, f"{version:064x}"),
    )


def _insert_overdraw_repair_fixture(
    database_path: Path,
    *,
    input_token_reservation: int | None,
    reserved_input_tokens: int,
    reserved_output_tokens: int,
    reserved_cost_usd: str,
    actual_input_tokens: int,
    actual_output_tokens: int,
    actual_cost_usd: str,
    state: str = "settled_actual",
) -> None:
    """Insert one old-semantics ledger projection at revision 0006."""

    if state not in {"settled_actual", "send_started"}:
        raise ValueError("unsupported overdraw repair fixture state")
    active = state == "send_started"
    settled_actual_input = None if active else actual_input_tokens
    settled_actual_output = None if active else actual_output_tokens
    settled_actual_cost = None if active else actual_cost_usd
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO governance_policies ("
            "id, version, policy_hash, is_active, backlog_limit, question_quantum, "
            "activated_at, created_at) VALUES ("
            "'policy-overdraw-repair', 1, ?, 1, 10, 2, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("a" * 64,),
        )
        scope_rows = (
            ("scope-overdraw-global", "global", "global"),
            ("scope-overdraw-provider", "provider", "b" * 64),
            ("scope-overdraw-model", "model", "model-1"),
            ("scope-overdraw-run", "run", "run-1"),
        )
        for scope_id, scope_type, scope_key in scope_rows:
            connection.execute(
                "INSERT INTO governance_scopes ("
                "id, scope_type, scope_key, active_reservations, reserved_requests, "
                "reserved_input_tokens, reserved_output_tokens, reserved_cost_usd, "
                "consumed_requests, consumed_input_tokens, consumed_output_tokens, "
                "consumed_cost_usd, overdrawn, created_at, updated_at) VALUES ("
                "?, ?, ?, ?, 0, ?, ?, ?, 1, ?, ?, ?, 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (
                    scope_id,
                    scope_type,
                    scope_key,
                    int(active),
                    reserved_input_tokens if active else 0,
                    reserved_output_tokens if active else 0,
                    reserved_cost_usd if active else "0",
                    settled_actual_input or 0,
                    settled_actual_output or 0,
                    settled_actual_cost or "0",
                ),
            )
            connection.execute(
                "INSERT INTO governance_minute_buckets ("
                "id, scope_id, policy_id, window_start, reserved_requests, "
                "reserved_input_tokens, reserved_output_tokens, consumed_requests, "
                "consumed_input_tokens, consumed_output_tokens, created_at, updated_at) "
                "VALUES (?, ?, 'policy-overdraw-repair', '2026-08-30 00:00:00', "
                "0, ?, ?, 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (
                    f"bucket-{scope_type}-overdraw-repair",
                    scope_id,
                    reserved_input_tokens if active else 0,
                    reserved_output_tokens if active else 0,
                    settled_actual_input or 0,
                    settled_actual_output or 0,
                ),
            )
        connection.execute(
            "UPDATE evaluation_runs SET governance_policy_id = 'policy-overdraw-repair', "
            "governance_status = 'managed', input_token_reservation = ? WHERE id = 'run-1'",
            (input_token_reservation,),
        )
        connection.execute(
            "INSERT INTO question_executions ("
            "id, run_id, question_id, execution_generation, next_provider_attempt, "
            "created_at, updated_at) VALUES ("
            "'execution-overdraw-repair', 'run-1', 'question-1', 0, 2, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO provider_call_reservations ("
            "id, operation_key, policy_id, question_execution_id, run_id, question_id, "
            "model_id, global_scope_id, provider_scope_id, model_scope_id, run_scope_id, "
            "execution_generation, provider_attempt, lease_owner, lease_token, state, "
            "window_start, reserved_input_tokens, reserved_output_tokens, reserved_cost_usd, "
            "actual_input_tokens, actual_output_tokens, actual_cost_usd, outcome_code, "
            "send_started_at, settled_at, created_at, updated_at) VALUES ("
            "'reservation-overdraw-repair', 'run-1:question-1:0:1', "
            "'policy-overdraw-repair', 'execution-overdraw-repair', 'run-1', 'question-1', "
            "'model-1', 'scope-overdraw-global', 'scope-overdraw-provider', "
            "'scope-overdraw-model', 'scope-overdraw-run', 0, 1, 'worker-overdraw-repair', "
            "1, ?, '2026-08-30 00:00:00', ?, ?, ?, ?, ?, ?, ?, "
            "'2026-08-30 00:00:01', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (
                state,
                reserved_input_tokens,
                reserved_output_tokens,
                reserved_cost_usd,
                settled_actual_input,
                settled_actual_output,
                settled_actual_cost,
                None if active else "succeeded",
                None if active else "2026-08-30 00:00:02",
            ),
        )


def _overdraw_repair_ledger_snapshot(database_path: Path) -> tuple[object, ...]:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT id, operation_key, state, reserved_input_tokens, "
            "reserved_output_tokens, reserved_cost_usd, actual_input_tokens, "
            "actual_output_tokens, actual_cost_usd, outcome_code, send_started_at, "
            "settled_at, created_at, updated_at FROM provider_call_reservations "
            "WHERE id = 'reservation-overdraw-repair'"
        ).fetchone()
    assert row is not None
    return row


def _mock_postgresql_historical_preflight(
    monkeypatch: pytest.MonkeyPatch,
    *,
    differences: tuple[str, ...],
    source_revision: str = WORKER_PROGRESS_REVISION,
) -> tuple[Mock, Mock, list[object]]:
    connection = Mock()
    connection.dialect.name = "postgresql"
    connection.in_transaction.return_value = False
    connection.execute.return_value.scalar_one.return_value = 0
    engine = Mock()
    engine.connect.return_value = connection
    inspector = Mock()
    inspector.get_table_names.return_value = list(Base.metadata.tables)
    metadata_calls: list[object] = []

    def schema_differences(candidate: object) -> tuple[str, ...]:
        metadata_calls.append(candidate)
        return differences

    monkeypatch.setattr(prepare_migrations_module, "create_database_engine", lambda _url: engine)
    monkeypatch.setattr(
        prepare_migrations_module,
        "database_heads",
        lambda _connection: (source_revision,),
    )
    monkeypatch.setattr(
        prepare_migrations_module,
        "expected_database_heads",
        lambda _config_path: (HEAD_REVISION,),
    )
    monkeypatch.setattr(prepare_migrations_module.sa, "inspect", lambda _connection: inspector)
    monkeypatch.setattr(prepare_migrations_module, "_schema_differences", schema_differences)
    return engine, connection, metadata_calls


def _rewrite_table_ddl(database_path: Path, table_name: str, old: str, new: str) -> None:
    """Inject one valid SQLite schema drift without rebuilding unrelated tables."""

    with sqlite3.connect(database_path) as connection:
        original = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()[0]
        assert old in original
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
            (original.replace(old, new), table_name),
        )
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")


def _rewrite_models_ddl(database_path: Path, old: str, new: str) -> None:
    _rewrite_table_ddl(database_path, "models", old, new)


def _rebuild_responses_without_primary_key(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'evaluation_responses'"
        ).fetchone()[0]
        index_sql = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'evaluation_responses' "
                "AND sql IS NOT NULL"
            )
        ]
        create_drifted_table = table_sql.replace(
            "CREATE TABLE evaluation_responses",
            "CREATE TABLE evaluation_responses_drift",
            1,
        ).replace("CONSTRAINT pk_evaluation_responses PRIMARY KEY (id), ", "", 1)
        assert create_drifted_table != table_sql
        connection.execute(create_drifted_table)
        connection.execute("DROP TABLE evaluation_responses")
        connection.execute("ALTER TABLE evaluation_responses_drift RENAME TO evaluation_responses")
        for statement in index_sql:
            connection.execute(statement)


def _rebuild_questions_without_rowid(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'questions'"
        ).fetchone()[0]
        index_sql = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'questions' AND sql IS NOT NULL"
            )
        ]
        create_drifted_table = (
            table_sql.replace(
                "CREATE TABLE questions",
                "CREATE TABLE questions_without_rowid",
                1,
            )
            + " WITHOUT ROWID"
        )
        connection.execute(create_drifted_table)
        connection.execute("DROP TABLE questions")
        connection.execute("ALTER TABLE questions_without_rowid RENAME TO questions")
        for statement in index_sql:
            connection.execute(statement)


def test_clean_migration_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "clean.db"

    _run_alembic(database_path, "upgrade", "head")
    assert _read_heads(database_path) == (HEAD_REVISION,)
    assert "No new upgrade operations detected" in _run_alembic(database_path, "check").stdout

    with sqlite3.connect(database_path) as connection:
        question_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(questions)")
        }
        assert question_columns["position"][3] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    _run_alembic(database_path, "downgrade", "base")
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not set(Base.metadata.tables) & tables

    _run_alembic(database_path, "upgrade", "head")
    assert _read_heads(database_path) == (HEAD_REVISION,)


def test_prepare_adopts_legacy_schema_and_preserves_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM alembic_version")

    result = prepare_database(_database_url(database_path))

    assert result.action == "stamped_legacy"
    assert result.stamped_revision == LEGACY_REVISION
    assert result.backup_path is not None and result.backup_path.is_file()
    assert _read_heads(database_path) == (LEGACY_REVISION,)
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 2
        assert "position" not in {row[1] for row in backup.execute("PRAGMA table_info(questions)")}
    versioned_result = prepare_database(_database_url(database_path))
    assert versioned_result.action == "versioned_legacy"
    assert versioned_result.backup_path is not None
    assert versioned_result.backup_path.is_file()

    _run_alembic(database_path, "upgrade", "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM evaluation_responses").fetchone()[0] == 1
        ordered_questions = connection.execute(
            "SELECT id, position FROM questions ORDER BY position"
        ).fetchall()
        assert ordered_questions == [("question-1", 0), ("question-2", 1)]
        model_columns = {row[1]: row for row in connection.execute("PRAGMA table_info(models)")}
        assert model_columns["input_price_per_million"][3] == 0
        assert model_columns["output_price_per_million"][3] == 0
        connection.execute(
            "UPDATE models SET input_price_per_million = NULL, "
            "output_price_per_million = NULL WHERE id = 'model-1'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE models SET base_url = 'https://invalid.example' WHERE id = 'model-1'"
            )
    assert _read_heads(database_path) == (HEAD_REVISION,)
    _run_alembic(database_path, "check")

    _run_alembic(database_path, "downgrade", LEGACY_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "position" not in {
            row[1] for row in connection.execute("PRAGMA table_info(questions)")
        }
        prices = connection.execute(
            "SELECT input_price_per_million, output_price_per_million "
            "FROM models WHERE id = 'model-1'"
        ).fetchone()
        assert prices == (0, 0)
        assert connection.execute("SELECT COUNT(*) FROM evaluation_responses").fetchone()[0] == 1
    downgrade_backup = prepare_database(_database_url(database_path))
    assert downgrade_backup.action == "versioned_legacy"
    assert downgrade_backup.backup_path is not None
    _run_alembic(database_path, "upgrade", "head")
    assert _read_heads(database_path) == (HEAD_REVISION,)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_prepare_adopts_current_unversioned_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "current.db"
    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM alembic_version")

    result = prepare_database(_database_url(database_path))

    assert result.action == "stamped_index_repair"
    assert result.stamped_revision == INDEX_REPAIR_REVISION
    assert result.backup_path is not None and result.backup_path.is_file()
    assert _read_heads(database_path) == (INDEX_REPAIR_REVISION,)
    _run_alembic(database_path, "upgrade", "head")
    assert _read_heads(database_path) == (HEAD_REVISION,)


def test_prepare_adopts_phase1_unversioned_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "phase1-unversioned.db"
    _run_alembic(database_path, "upgrade", PHASE_1_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM alembic_version")

    result = prepare_database(_database_url(database_path))

    assert result.action == "stamped_phase1"
    assert result.stamped_revision == PHASE_1_REVISION
    assert result.backup_path is not None and result.backup_path.is_file()
    assert _read_heads(database_path) == (PHASE_1_REVISION,)

    versioned_result = prepare_database(_database_url(database_path))
    assert versioned_result.action == "versioned_phase1"
    assert versioned_result.stamped_revision == PHASE_1_REVISION
    assert versioned_result.backup_path is not None and versioned_result.backup_path.is_file()

    _run_alembic(database_path, "upgrade", "head")
    assert _read_heads(database_path) == (HEAD_REVISION,)
    _run_alembic(database_path, "check")


def test_prepare_adopts_reliability_unversioned_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "reliability-unversioned.db"
    _run_alembic(database_path, "upgrade", RELIABILITY_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM alembic_version")

    result = prepare_database(_database_url(database_path))

    assert result.action == "stamped_reliability"
    assert result.stamped_revision == RELIABILITY_REVISION
    assert result.backup_path is not None and result.backup_path.is_file()
    assert _read_heads(database_path) == (RELIABILITY_REVISION,)

    versioned_result = prepare_database(_database_url(database_path))
    assert versioned_result.action == "versioned_reliability"
    assert versioned_result.stamped_revision == RELIABILITY_REVISION
    assert versioned_result.backup_path is not None and versioned_result.backup_path.is_file()

    _run_alembic(database_path, "upgrade", "head")
    assert _read_heads(database_path) == (HEAD_REVISION,)
    _run_alembic(database_path, "check")


@pytest.mark.parametrize("versioned", [False, True])
def test_prepare_backs_up_and_adopts_web_credential_schema(
    tmp_path: Path,
    versioned: bool,
) -> None:
    database_path = tmp_path / f"web-credentials-{'versioned' if versioned else 'unversioned'}.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", CREDENTIAL_REVISION)
    if not versioned:
        with sqlite3.connect(database_path) as connection:
            connection.execute("DELETE FROM alembic_version")

    result = prepare_database(_database_url(database_path))

    assert result.action == ("versioned_credentials" if versioned else "stamped_credentials")
    assert result.stamped_revision == CREDENTIAL_REVISION
    assert result.backup_path is not None and result.backup_path.is_file()
    assert _read_heads(database_path) == (CREDENTIAL_REVISION,)
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 1
        tables = {
            row[0] for row in backup.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "model_credentials" in tables
        assert "governance_policies" not in tables

    _run_alembic(database_path, "upgrade", "head")
    assert _read_heads(database_path) == (HEAD_REVISION,)
    _run_alembic(database_path, "check")


@pytest.mark.parametrize("versioned", [False, True])
def test_prepare_backs_up_and_adopts_governance_schema(
    tmp_path: Path,
    versioned: bool,
) -> None:
    database_path = tmp_path / f"governance-{'versioned' if versioned else 'unversioned'}.db"
    _run_alembic(database_path, "upgrade", GOVERNANCE_REVISION)
    if not versioned:
        with sqlite3.connect(database_path) as connection:
            connection.execute("DELETE FROM alembic_version")

    result = prepare_database(_database_url(database_path))

    assert result.action == ("versioned_governance" if versioned else "stamped_governance")
    assert result.stamped_revision == GOVERNANCE_REVISION
    assert result.backup_path is not None and result.backup_path.is_file()
    assert _read_heads(database_path) == (GOVERNANCE_REVISION,)
    with sqlite3.connect(result.backup_path) as backup:
        tables = {
            row[0] for row in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row[0] for row in backup.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "audit_events" in tables
        assert "worker_processes" not in tables
        assert "ix_audit_events_expires_id" not in indexes
        assert "ix_audit_events_occurred_id" not in indexes

    _run_alembic(database_path, "upgrade", "head")
    assert _read_heads(database_path) == (HEAD_REVISION,)
    _run_alembic(database_path, "check")


def test_known_governance_index_gap_upgrades_through_0005_and_0006(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "known-governance-index-gap.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", GOVERNANCE_REVISION)
    with sqlite3.connect(database_path) as connection:
        _insert_active_policy(connection, policy_id="policy-active", version=1)
        data_counts_before = connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM models), "
            "(SELECT COUNT(*) FROM benchmarks), "
            "(SELECT COUNT(*) FROM questions), "
            "(SELECT COUNT(*) FROM evaluation_runs), "
            "(SELECT COUNT(*) FROM evaluation_responses), "
            "(SELECT COUNT(*) FROM governance_policies)"
        ).fetchone()
    _drop_known_governance_indexes(database_path)

    governance_result = prepare_database(_database_url(database_path))

    assert governance_result.action == "versioned_governance"
    assert governance_result.backup_path is not None
    assert governance_result.backup_path.is_file()
    assert _read_heads(database_path) == (GOVERNANCE_REVISION,)
    with sqlite3.connect(database_path) as connection:
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert (
            not {
                "ix_evaluation_runs_started_at_id",
                "ix_evaluation_runs_finished_at_id",
                "uq_governance_policies_single_active",
            }
            & indexes
        )

    _run_alembic(database_path, "upgrade", WORKER_PROGRESS_REVISION)

    assert _read_heads(database_path) == (WORKER_PROGRESS_REVISION,)
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "worker_processes" in tables
        assert {
            "ix_worker_processes_stopped_seen_generation",
            "ix_audit_events_expires_id",
            "ix_audit_events_occurred_id",
        } <= indexes
        assert (
            not {
                "ix_evaluation_runs_started_at_id",
                "ix_evaluation_runs_finished_at_id",
                "uq_governance_policies_single_active",
            }
            & indexes
        )

    worker_progress_result = prepare_database(_database_url(database_path))

    assert worker_progress_result.action == "versioned_worker_progress"
    assert worker_progress_result.backup_path is not None
    assert worker_progress_result.backup_path.is_file()
    _run_alembic(database_path, "upgrade", "head")

    assert _read_heads(database_path) == (HEAD_REVISION,)
    with sqlite3.connect(database_path) as connection:
        data_counts_after = connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM models), "
            "(SELECT COUNT(*) FROM benchmarks), "
            "(SELECT COUNT(*) FROM questions), "
            "(SELECT COUNT(*) FROM evaluation_runs), "
            "(SELECT COUNT(*) FROM evaluation_responses), "
            "(SELECT COUNT(*) FROM governance_policies)"
        ).fetchone()
        assert data_counts_after == data_counts_before
        for index_name, expected_columns in (
            ("ix_evaluation_runs_started_at_id", ["started_at", "id"]),
            ("ix_evaluation_runs_finished_at_id", ["finished_at", "id"]),
        ):
            assert [
                row[2] for row in connection.execute(f'PRAGMA index_info("{index_name}")')
            ] == expected_columns
        active_index = {
            row[1]: row for row in connection.execute("PRAGMA index_list(governance_policies)")
        }["uq_governance_policies_single_active"]
        assert active_index[2] == 1
        assert active_index[4] == 1
        active_index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='uq_governance_policies_single_active'"
        ).fetchone()[0]
        assert active_index_sql.endswith("WHERE is_active = 1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_active_policy(connection, policy_id="policy-active-second", version=2)
        connection.rollback()
    _run_alembic(database_path, "check")

    _run_alembic(database_path, "downgrade", WORKER_PROGRESS_REVISION)

    assert _read_heads(database_path) == (WORKER_PROGRESS_REVISION,)
    with sqlite3.connect(database_path) as connection:
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {
            "ix_evaluation_runs_started_at_id",
            "ix_evaluation_runs_finished_at_id",
            "uq_governance_policies_single_active",
        } <= indexes
    canonical_0005_result = prepare_database(_database_url(database_path))
    assert canonical_0005_result.action == "versioned_worker_progress"
    _run_alembic(database_path, "upgrade", "head")
    assert _read_heads(database_path) == (HEAD_REVISION,)


def test_known_governance_index_gap_resumes_after_partial_0006_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "partial-governance-index-repair.db"
    _run_alembic(database_path, "upgrade", WORKER_PROGRESS_REVISION)
    _drop_known_governance_indexes(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE INDEX ix_evaluation_runs_started_at_id ON evaluation_runs (started_at, id)"
        )

    result = prepare_database(_database_url(database_path))

    assert result.action == "versioned_worker_progress"
    assert result.backup_path is not None and result.backup_path.is_file()
    assert _read_heads(database_path) == (WORKER_PROGRESS_REVISION,)
    _run_alembic(database_path, "upgrade", "head")

    assert _read_heads(database_path) == (HEAD_REVISION,)
    with sqlite3.connect(database_path) as connection:
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {
            "ix_evaluation_runs_started_at_id",
            "ix_evaluation_runs_finished_at_id",
            "uq_governance_policies_single_active",
        } <= indexes
    _run_alembic(database_path, "check")


def test_observational_overdraw_repair_preserves_ledger_and_is_reversible(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "observational-overdraw-repair.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", INDEX_REPAIR_REVISION)
    _insert_overdraw_repair_fixture(
        database_path,
        input_token_reservation=None,
        reserved_input_tokens=59,
        reserved_output_tokens=128,
        reserved_cost_usd="0.00010000",
        actual_input_tokens=75,
        actual_output_tokens=64,
        actual_cost_usd="0.00020000",
    )
    ledger_before = _overdraw_repair_ledger_snapshot(database_path)
    with sqlite3.connect(database_path) as connection:
        counters_before = connection.execute(
            "SELECT active_reservations, reserved_requests, reserved_input_tokens, "
            "reserved_output_tokens, reserved_cost_usd, consumed_requests, "
            "consumed_input_tokens, consumed_output_tokens, consumed_cost_usd "
            "FROM governance_scopes ORDER BY id"
        ).fetchall()
        assert (
            connection.execute("SELECT COUNT(*) FROM governance_scopes WHERE overdrawn").fetchone()[
                0
            ]
            == 4
        )

    _run_alembic(database_path, "upgrade", "head")

    assert _read_heads(database_path) == (HEAD_REVISION,)
    assert _overdraw_repair_ledger_snapshot(database_path) == ledger_before
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM governance_scopes WHERE overdrawn").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT active_reservations, reserved_requests, reserved_input_tokens, "
                "reserved_output_tokens, reserved_cost_usd, consumed_requests, "
                "consumed_input_tokens, consumed_output_tokens, consumed_cost_usd "
                "FROM governance_scopes ORDER BY id"
            ).fetchall()
            == counters_before
        )

    _run_alembic(database_path, "downgrade", INDEX_REPAIR_REVISION)

    assert _read_heads(database_path) == (INDEX_REPAIR_REVISION,)
    assert _overdraw_repair_ledger_snapshot(database_path) == ledger_before
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM governance_scopes WHERE overdrawn").fetchone()[
                0
            ]
            == 4
        )


@pytest.mark.parametrize(
    (
        "case_name",
        "input_token_reservation",
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reserved_cost_usd",
        "actual_input_tokens",
        "actual_output_tokens",
        "actual_cost_usd",
    ),
    [
        ("input", 59, 59, 128, "0.00020000", 75, 64, "0.00010000"),
        ("output", None, 59, 4, "0.00020000", 40, 5, "0.00010000"),
        ("cost", 128, 128, 128, "0.00010000", 40, 64, "0.00020000"),
    ],
)
def test_overdraw_repair_preserves_explicit_hard_bound_overdraw(
    tmp_path: Path,
    case_name: str,
    input_token_reservation: int | None,
    reserved_input_tokens: int,
    reserved_output_tokens: int,
    reserved_cost_usd: str,
    actual_input_tokens: int,
    actual_output_tokens: int,
    actual_cost_usd: str,
) -> None:
    database_path = tmp_path / f"explicit-{case_name}-overdraw-repair.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", INDEX_REPAIR_REVISION)
    _insert_overdraw_repair_fixture(
        database_path,
        input_token_reservation=input_token_reservation,
        reserved_input_tokens=reserved_input_tokens,
        reserved_output_tokens=reserved_output_tokens,
        reserved_cost_usd=reserved_cost_usd,
        actual_input_tokens=actual_input_tokens,
        actual_output_tokens=actual_output_tokens,
        actual_cost_usd=actual_cost_usd,
    )
    ledger_before = _overdraw_repair_ledger_snapshot(database_path)

    _run_alembic(database_path, "upgrade", "head")

    assert _read_heads(database_path) == (HEAD_REVISION,)
    assert _overdraw_repair_ledger_snapshot(database_path) == ledger_before
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM governance_scopes WHERE overdrawn").fetchone()[
                0
            ]
            == 4
        )


@pytest.mark.parametrize(
    ("start_revision", "direction", "target_revision"),
    [
        (INDEX_REPAIR_REVISION, "upgrade", "head"),
        (HEAD_REVISION, "downgrade", INDEX_REPAIR_REVISION),
    ],
)
def test_overdraw_repair_refuses_active_reservations_before_mutation(
    tmp_path: Path,
    start_revision: str,
    direction: str,
    target_revision: str,
) -> None:
    database_path = tmp_path / f"active-overdraw-{direction}.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", start_revision)
    _insert_overdraw_repair_fixture(
        database_path,
        input_token_reservation=None,
        reserved_input_tokens=59,
        reserved_output_tokens=128,
        reserved_cost_usd="0.00010000",
        actual_input_tokens=75,
        actual_output_tokens=64,
        actual_cost_usd="0.00020000",
        state="send_started",
    )
    ledger_before = _overdraw_repair_ledger_snapshot(database_path)
    with sqlite3.connect(database_path) as connection:
        scope_rows_before = connection.execute(
            "SELECT id, active_reservations, reserved_input_tokens, "
            "reserved_output_tokens, reserved_cost_usd, overdrawn "
            "FROM governance_scopes ORDER BY id"
        ).fetchall()

    failed = _invoke_alembic(database_path, direction, target_revision)

    assert failed.returncode != 0
    assert "Provider reservations are active" in failed.stderr
    assert _read_heads(database_path) == (start_revision,)
    assert _overdraw_repair_ledger_snapshot(database_path) == ledger_before
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT id, active_reservations, reserved_input_tokens, "
                "reserved_output_tokens, reserved_cost_usd, overdrawn "
                "FROM governance_scopes ORDER BY id"
            ).fetchall()
            == scope_rows_before
        )


@pytest.mark.parametrize(
    ("differences", "expected_row_guard_calls"),
    [
        (
            (),
            0,
        ),
        (
            ("add_index:evaluation_runs.ix_evaluation_runs_started_at_id",),
            1,
        ),
        (
            (
                "add_index:evaluation_runs.ix_evaluation_runs_finished_at_id",
                "add_index:evaluation_runs.ix_evaluation_runs_started_at_id",
                "add_index:governance_policies.uq_governance_policies_single_active",
            ),
            1,
        ),
    ],
)
def test_prepare_postgresql_0005_accepts_only_canonical_or_known_index_gap(
    monkeypatch: pytest.MonkeyPatch,
    differences: tuple[str, ...],
    expected_row_guard_calls: int,
) -> None:
    engine, connection, metadata_calls = _mock_postgresql_historical_preflight(
        monkeypatch,
        differences=differences,
    )

    result = prepare_database("postgresql+psycopg://localhost/llmbenchlab_test")

    assert result.action == "versioned"
    assert metadata_calls == [connection]
    assert connection.execute.call_count == expected_row_guard_calls
    connection.close.assert_called_once_with()
    engine.dispose.assert_called_once_with()


def test_prepare_postgresql_0005_rejects_unknown_drift_with_known_index_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, connection, metadata_calls = _mock_postgresql_historical_preflight(
        monkeypatch,
        differences=(
            "add_index:evaluation_runs.ix_evaluation_runs_started_at_id",
            "add_index:models.ix_models_enabled",
        ),
    )

    with pytest.raises(SchemaPreparationError, match="historical database"):
        prepare_database("postgresql+psycopg://localhost/llmbenchlab_test")

    assert metadata_calls == [connection]
    connection.execute.assert_not_called()
    connection.close.assert_called_once_with()
    engine.dispose.assert_called_once_with()


def test_prepare_postgresql_0006_checks_canonical_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, connection, metadata_calls = _mock_postgresql_historical_preflight(
        monkeypatch,
        differences=(),
        source_revision=INDEX_REPAIR_REVISION,
    )

    result = prepare_database("postgresql+psycopg://localhost/llmbenchlab_test")

    assert result.action == "versioned"
    assert metadata_calls == [connection]
    connection.execute.assert_not_called()
    connection.close.assert_called_once_with()
    engine.dispose.assert_called_once_with()


def test_prepare_postgresql_0006_rejects_metadata_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, connection, metadata_calls = _mock_postgresql_historical_preflight(
        monkeypatch,
        differences=("add_index:models.ix_models_enabled",),
        source_revision=INDEX_REPAIR_REVISION,
    )

    with pytest.raises(SchemaPreparationError, match="historical database"):
        prepare_database("postgresql+psycopg://localhost/llmbenchlab_test")

    assert metadata_calls == [connection]
    connection.execute.assert_not_called()
    connection.close.assert_called_once_with()
    engine.dispose.assert_called_once_with()


def test_known_governance_index_gap_rejects_duplicate_active_policies(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "duplicate-active-policy-index-gap.db"
    _run_alembic(database_path, "upgrade", GOVERNANCE_REVISION)
    _drop_known_governance_indexes(database_path)
    with sqlite3.connect(database_path) as connection:
        _insert_active_policy(connection, policy_id="policy-active-first", version=1)
        _insert_active_policy(connection, policy_id="policy-active-second", version=2)

    with pytest.raises(SchemaPreparationError, match="multiple active policies"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == (GOVERNANCE_REVISION,)
    assert list(tmp_path.glob("*.bak")) == []


def test_governance_index_repair_migration_guards_duplicate_active_policies_before_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "direct-upgrade-duplicate-active-policy.db"
    _run_alembic(database_path, "upgrade", WORKER_PROGRESS_REVISION)
    _drop_known_governance_indexes(database_path)
    with sqlite3.connect(database_path) as connection:
        _insert_active_policy(connection, policy_id="policy-active-first", version=1)
        _insert_active_policy(connection, policy_id="policy-active-second", version=2)

    failed = _invoke_alembic(database_path, "upgrade", "head")

    assert failed.returncode != 0
    assert "multiple active policies exist" in failed.stderr
    assert _read_heads(database_path) == (WORKER_PROGRESS_REVISION,)
    with sqlite3.connect(database_path) as connection:
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert (
            not {
                "ix_evaluation_runs_started_at_id",
                "ix_evaluation_runs_finished_at_id",
                "uq_governance_policies_single_active",
            }
            & indexes
        )


def test_governance_index_repair_rejects_same_name_partial_run_index(tmp_path: Path) -> None:
    database_path = tmp_path / "direct-upgrade-partial-run-index.db"
    _run_alembic(database_path, "upgrade", WORKER_PROGRESS_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX ix_evaluation_runs_started_at_id")
        connection.execute(
            "CREATE INDEX ix_evaluation_runs_started_at_id "
            "ON evaluation_runs (started_at, id) WHERE started_at IS NOT NULL"
        )

    failed = _invoke_alembic(database_path, "upgrade", "head")

    assert failed.returncode != 0
    assert "does not match the repair migration predicate" in failed.stderr
    assert _read_heads(database_path) == (WORKER_PROGRESS_REVISION,)
    with sqlite3.connect(database_path) as connection:
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='ix_evaluation_runs_started_at_id'"
        ).fetchone()[0]
        assert index_sql.endswith("WHERE started_at IS NOT NULL")


def test_known_governance_index_gap_does_not_allow_additional_drift(tmp_path: Path) -> None:
    database_path = tmp_path / "governance-index-gap-with-extra-drift.db"
    _run_alembic(database_path, "upgrade", GOVERNANCE_REVISION)
    _drop_known_governance_indexes(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX ix_models_enabled")

    with pytest.raises(SchemaPreparationError, match="historical database"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == (GOVERNANCE_REVISION,)
    assert list(tmp_path.glob("*.bak")) == []


@pytest.mark.parametrize(
    ("cancellation_requested", "expected_status", "expected_last_error"),
    [
        (False, "failed", "migrated_interrupted_run"),
        (True, "cancelled", "migrated_cancelled_run"),
    ],
)
def test_reliability_upgrade_settles_nonresumable_phase1_running_run(
    tmp_path: Path,
    cancellation_requested: bool,
    expected_status: str,
    expected_last_error: str,
) -> None:
    database_path = tmp_path / f"running-phase1-{expected_status}.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", PHASE_1_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE evaluation_runs SET status = 'running', cancellation_requested = ?, "
            "model_parameters_snapshot = ?, started_at = CURRENT_TIMESTAMP WHERE id = 'run-1'",
            (
                cancellation_requested,
                '{"execution":{"restart_recovery":"mark_failed_without_resume"}}',
            ),
        )
        snapshot_before = connection.execute(
            "SELECT protocol_version, model_parameters_snapshot FROM evaluation_runs "
            "WHERE id = 'run-1'"
        ).fetchone()
        response_before = connection.execute(
            "SELECT id, run_id, question_id, score FROM evaluation_responses "
            "WHERE id = 'response-1'"
        ).fetchone()

    _run_alembic(database_path, "upgrade", "head")

    with sqlite3.connect(database_path) as connection:
        aggregate = connection.execute(
            "SELECT total_questions, completed_questions, correct_questions, error_questions, "
            "score, completion_rate, answered_accuracy, average_latency_ms, input_tokens, "
            "output_tokens, estimated_cost FROM evaluation_runs WHERE id = 'run-1'"
        ).fetchone()
        reliability = connection.execute(
            "SELECT status, attempt_count, max_attempts, lease_token, lease_owner, "
            "lease_expires_at, heartbeat_at, next_attempt_at, last_error, "
            "cancellation_requested, finished_at IS NOT NULL, error_message "
            "FROM evaluation_runs WHERE id = 'run-1'"
        ).fetchone()
        snapshot_after = connection.execute(
            "SELECT protocol_version, model_parameters_snapshot FROM evaluation_runs "
            "WHERE id = 'run-1'"
        ).fetchone()
        response_after = connection.execute(
            "SELECT id, run_id, question_id, score FROM evaluation_responses "
            "WHERE id = 'response-1'"
        ).fetchone()
    assert aggregate == (2, 1, 1, 0, 50.0, 0.0, None, None, None, None, None)
    assert snapshot_after == snapshot_before
    assert response_after == response_before
    assert reliability == (
        expected_status,
        0,
        3,
        0,
        None,
        None,
        None,
        None,
        expected_last_error,
        cancellation_requested,
        1,
        None if cancellation_requested else "interrupted_by_reliability_migration",
    )


def test_web_credential_migration_backfills_sources_and_refuses_lossy_downgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "web-credentials.db"
    _run_alembic(database_path, "upgrade", RELIABILITY_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO models (
                id, name, provider_type, base_url, remote_model_name, api_key_env,
                enabled, input_price_per_million, output_price_per_million,
                default_parameters, created_at, updated_at
            ) VALUES (
                'model-environment', 'Environment Provider', 'openai_compatible',
                'https://provider.example/v1', 'provider-model', 'PROVIDER_KEY',
                1, NULL, NULL, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO models (
                id, name, provider_type, base_url, remote_model_name, api_key_env,
                enabled, input_price_per_million, output_price_per_million,
                default_parameters, created_at, updated_at
            ) VALUES (
                'model-mock', 'Mock Provider', 'mock', NULL, NULL, NULL,
                1, 0, 0, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )

    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT id, credential_source FROM models ORDER BY id"
        ).fetchall() == [
            ("model-environment", "environment"),
            ("model-mock", "none"),
        ]
        connection.execute(
            "UPDATE models SET credential_source = 'stored', api_key_env = NULL "
            "WHERE id = 'model-environment'"
        )
        connection.execute(
            """
            INSERT INTO model_credentials (
                model_id, algorithm, key_id, nonce, ciphertext, created_at, updated_at
            ) VALUES (
                'model-environment', 'aes-256-gcm-v1', 'fixture-v1', ?, ?,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """,
            (sqlite3.Binary(b"n" * 12), sqlite3.Binary(b"ciphertext-is-not-plaintext")),
        )

    failed = _invoke_alembic(database_path, "downgrade", RELIABILITY_REVISION)

    assert failed.returncode != 0
    assert "Cannot downgrade while encrypted Web credentials exist" in failed.stderr
    assert _read_heads(database_path) == (WEB_CREDENTIAL_REVISION,)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT model_id, algorithm, key_id, nonce, ciphertext FROM model_credentials"
        ).fetchone() == (
            "model-environment",
            "aes-256-gcm-v1",
            "fixture-v1",
            b"n" * 12,
            b"ciphertext-is-not-plaintext",
        )
        assert connection.execute(
            "SELECT credential_source, api_key_env FROM models WHERE id = 'model-environment'"
        ).fetchone() == ("stored", None)


def test_governance_schema_matches_orm_and_does_not_seed_policy(tmp_path: Path) -> None:
    database_path = tmp_path / "governance-schema.db"

    _run_alembic(database_path, "upgrade", "head")

    expected_tables = {
        "governance_policies",
        "governance_scopes",
        "governance_minute_buckets",
        "question_executions",
        "provider_call_reservations",
        "audit_events",
        "worker_processes",
    }
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert expected_tables <= tables
        worker_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(worker_processes)")
        }
        assert worker_columns == {
            "generation_id",
            "worker_id",
            "started_at",
            "last_seen_at",
            "last_scan_at",
            "last_claim_at",
            "last_progress_at",
            "last_lease_heartbeat_at",
            "stopped_at",
        }
        worker_indexes = {
            row[1]: row for row in connection.execute("PRAGMA index_list(worker_processes)")
        }
        assert worker_indexes["ix_worker_processes_stopped_seen_generation"][2] == 0
        assert [
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(ix_worker_processes_stopped_seen_generation)"
            )
        ] == ["stopped_at", "last_seen_at", "generation_id"]
        audit_indexes = {
            row[1]: row for row in connection.execute("PRAGMA index_list(audit_events)")
        }
        for index_name, expected_columns in (
            ("ix_audit_events_expires_id", ["expires_at", "id"]),
            ("ix_audit_events_occurred_id", ["occurred_at", "id"]),
        ):
            assert audit_indexes[index_name][2] == 0
            assert [
                row[2] for row in connection.execute(f'PRAGMA index_info("{index_name}")')
            ] == expected_columns
        audit_window_plan = [
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM audit_events "
                "WHERE occurred_at >= ? AND occurred_at < ? "
                "ORDER BY occurred_at, id LIMIT 50001",
                ("2026-08-28 00:00:00", "2026-08-28 00:15:00"),
            )
        ]
        assert any("ix_audit_events_occurred_id" in detail for detail in audit_window_plan)
        assert all("TEMP B-TREE" not in detail for detail in audit_window_plan)
        assert connection.execute("SELECT COUNT(*) FROM governance_policies").fetchone()[0] == 0
        policy_indexes = {
            row[1]: row for row in connection.execute("PRAGMA index_list(governance_policies)")
        }
        active_index = policy_indexes["uq_governance_policies_single_active"]
        assert active_index[2] == 1
        assert active_index[4] == 1
        assert [
            row[2]
            for row in connection.execute("PRAGMA index_info(uq_governance_policies_single_active)")
        ] == ["is_active"]

        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(evaluation_runs)")}
        assert {
            "failed_attempt_count",
            "dispatch_count",
            "last_scheduled_at",
            "governance_policy_id",
            "governance_status",
            "governance_reason",
            "governance_not_before",
            "input_token_reservation",
            "lifetime_request_budget",
            "lifetime_token_budget",
            "lifetime_cost_budget_usd",
        } <= run_columns
        run_indexes = {row[1] for row in connection.execute("PRAGMA index_list(evaluation_runs)")}
        for index_name, expected_columns in (
            ("ix_evaluation_runs_started_at_id", ["started_at", "id"]),
            ("ix_evaluation_runs_finished_at_id", ["finished_at", "id"]),
        ):
            assert index_name in run_indexes
            assert [
                row[2] for row in connection.execute(f'PRAGMA index_info("{index_name}")')
            ] == expected_columns
        response_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(evaluation_responses)")
        }
        assert {
            "provider_request_id",
            "returned_model",
            "system_fingerprint",
            "finish_reason",
            "http_attempt_count",
        } <= response_columns

        reservation_parents = {
            (row[2], row[6])
            for row in connection.execute("PRAGMA foreign_key_list(provider_call_reservations)")
        }
        assert reservation_parents == {
            ("governance_policies", "RESTRICT"),
            ("question_executions", "RESTRICT"),
            ("evaluation_runs", "RESTRICT"),
            ("questions", "RESTRICT"),
            ("models", "RESTRICT"),
            ("governance_scopes", "RESTRICT"),
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert "No new upgrade operations detected" in _run_alembic(database_path, "check").stdout


def test_governance_migration_rejects_two_active_policies(tmp_path: Path) -> None:
    database_path = tmp_path / "governance-single-active.db"
    _run_alembic(database_path, "upgrade", "head")
    insert_policy = (
        "INSERT INTO governance_policies ("
        "id, version, policy_hash, is_active, backlog_limit, question_quantum, "
        "activated_at, created_at"
        ") VALUES (?, ?, ?, ?, 100, 25, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute(insert_policy, ("policy-active-a", 1, "a" * 64, 1))
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert_policy, ("policy-active-b", 2, "b" * 64, 1))
        connection.rollback()
        connection.execute(insert_policy, ("policy-inactive-b", 2, "b" * 64, 0))
        connection.execute(insert_policy, ("policy-inactive-c", 3, "c" * 64, 0))
        connection.commit()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM governance_policies WHERE is_active = 1"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM governance_policies").fetchone()[0] == 3


@pytest.mark.parametrize(
    ("status_value", "attempt_count", "expected_failed_count"),
    [
        ("pending", 3, 3),
        ("running", 3, 2),
        ("running", 1, 0),
    ],
)
def test_governance_upgrade_backfills_failed_attempts_by_run_state(
    tmp_path: Path,
    status_value: str,
    attempt_count: int,
    expected_failed_count: int,
) -> None:
    database_path = tmp_path / f"governance-backfill-{status_value}-{attempt_count}.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", WEB_CREDENTIAL_REVISION)

    with sqlite3.connect(database_path) as connection:
        if status_value == "running":
            connection.execute(
                "UPDATE evaluation_runs SET status = 'running', attempt_count = ?, "
                "max_attempts = 3, lease_owner = 'legacy-worker', lease_token = 7, "
                "lease_expires_at = datetime('now', '+1 hour'), "
                "heartbeat_at = CURRENT_TIMESTAMP WHERE id = 'run-1'",
                (attempt_count,),
            )
        else:
            connection.execute(
                "UPDATE evaluation_runs SET status = 'pending', attempt_count = ?, "
                "max_attempts = 3 WHERE id = 'run-1'",
                (attempt_count,),
            )

    _run_alembic(database_path, "upgrade", "head")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT attempt_count, max_attempts, failed_attempt_count, dispatch_count, "
            "governance_status, governance_policy_id FROM evaluation_runs WHERE id = 'run-1'"
        ).fetchone() == (
            attempt_count,
            3,
            expected_failed_count,
            0,
            "legacy_unmanaged",
            None,
        )
        # Claim/slice count is intentionally no longer bounded by the failure budget.
        connection.execute("UPDATE evaluation_runs SET attempt_count = 9 WHERE id = 'run-1'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE evaluation_runs SET failed_attempt_count = 4 WHERE id = 'run-1'"
            )


def test_worker_progress_downgrade_rejects_process_facts_before_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "worker-progress-downgrade.db"
    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO worker_processes (generation_id, worker_id, started_at, last_seen_at) "
            "VALUES ('00000000-0000-0000-0000-000000000001', 'worker-test', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    failed = _invoke_alembic(database_path, "downgrade", GOVERNANCE_REVISION)

    assert failed.returncode != 0
    assert "process facts exist" in failed.stderr
    # Revision 0006 has a schema-no-op downgrade, so the 0005 evidence guard
    # still refuses before any Worker table or index DDL is attempted.
    assert _read_heads(database_path) == (WORKER_PROGRESS_REVISION,)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM worker_processes").fetchone()[0] == 1
        audit_indexes = {row[1] for row in connection.execute("PRAGMA index_list(audit_events)")}
        assert "ix_audit_events_expires_id" in audit_indexes
        assert "ix_audit_events_occurred_id" in audit_indexes


def test_governance_downgrade_rejects_response_metadata_before_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "governance-metadata-downgrade.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE evaluation_responses SET provider_request_id = 'safe-request-id', "
            "http_attempt_count = 1 WHERE id = 'response-1'"
        )

    failed = _invoke_alembic(database_path, "downgrade", WEB_CREDENTIAL_REVISION)

    assert failed.returncode != 0
    assert "Response Provider metadata exists" in failed.stderr
    assert _read_heads(database_path) == (GOVERNANCE_REVISION,)
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "audit_events" in tables
        assert "provider_call_reservations" in tables
        assert "failed_attempt_count" in {
            row[1] for row in connection.execute("PRAGMA table_info(evaluation_runs)")
        }
        assert "provider_request_id" in {
            row[1] for row in connection.execute("PRAGMA table_info(evaluation_responses)")
        }


def test_governance_downgrade_rejects_failure_and_fairness_evidence_before_ddl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "governance-run-evidence-downgrade.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE evaluation_runs SET failed_attempt_count = 1, dispatch_count = 1, "
            "last_scheduled_at = CURRENT_TIMESTAMP WHERE id = 'run-1'"
        )

    failed = _invoke_alembic(database_path, "downgrade", WEB_CREDENTIAL_REVISION)

    assert failed.returncode != 0
    assert "Run governance/failure evidence" in failed.stderr
    assert _read_heads(database_path) == (GOVERNANCE_REVISION,)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT failed_attempt_count, dispatch_count FROM evaluation_runs WHERE id = 'run-1'"
        ).fetchone() == (1, 1)
        assert "audit_events" in {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def test_governance_downgrade_rejects_question_execution_before_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "governance-question-execution-downgrade.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO question_executions ("
            "id, run_id, question_id, execution_generation, next_provider_attempt, "
            "created_at, updated_at) VALUES ("
            "'execution-1', 'run-1', 'question-1', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    failed = _invoke_alembic(database_path, "downgrade", WEB_CREDENTIAL_REVISION)

    assert failed.returncode != 0
    assert "question-execution evidence exists" in failed.stderr
    assert _read_heads(database_path) == (GOVERNANCE_REVISION,)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT run_id, question_id FROM question_executions WHERE id = 'execution-1'"
        ).fetchone() == ("run-1", "question-1")


def test_governance_ledger_foreign_keys_are_never_delete_restrict(tmp_path: Path) -> None:
    database_path = tmp_path / "governance-ledger-restrict.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO governance_policies ("
            "id, version, policy_hash, is_active, backlog_limit, question_quantum, "
            "activated_at, created_at) VALUES ("
            "'policy-1', 1, ?, 1, 10, 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("a" * 64,),
        )
        for scope_id, scope_type, scope_key in (
            ("scope-global", "global", "global"),
            ("scope-provider", "provider", "b" * 64),
            ("scope-model", "model", "model-1"),
            ("scope-run", "run", "run-1"),
        ):
            connection.execute(
                "INSERT INTO governance_scopes ("
                "id, scope_type, scope_key, active_reservations, reserved_requests, "
                "reserved_input_tokens, reserved_output_tokens, reserved_cost_usd, "
                "consumed_requests, consumed_input_tokens, consumed_output_tokens, "
                "consumed_cost_usd, overdrawn, created_at, updated_at) VALUES ("
                "?, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (scope_id, scope_type, scope_key),
            )
        connection.execute(
            "UPDATE evaluation_runs SET governance_policy_id = 'policy-1', "
            "governance_status = 'managed' WHERE id = 'run-1'"
        )
        connection.execute(
            "INSERT INTO question_executions ("
            "id, run_id, question_id, execution_generation, next_provider_attempt, "
            "created_at, updated_at) VALUES ("
            "'execution-1', 'run-1', 'question-1', 0, 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO provider_call_reservations ("
            "id, operation_key, policy_id, question_execution_id, run_id, question_id, "
            "model_id, global_scope_id, provider_scope_id, model_scope_id, run_scope_id, "
            "execution_generation, provider_attempt, lease_owner, lease_token, state, "
            "window_start, reserved_input_tokens, reserved_output_tokens, reserved_cost_usd, "
            "actual_input_tokens, actual_output_tokens, actual_cost_usd, outcome_code, "
            "send_started_at, settled_at, created_at, updated_at) VALUES ("
            "'reservation-1', 'run-1:question-1:0:1', 'policy-1', 'execution-1', "
            "'run-1', 'question-1', 'model-1', 'scope-global', 'scope-provider', "
            "'scope-model', 'scope-run', 0, 1, 'worker-1', 1, 'settled_actual', "
            "CURRENT_TIMESTAMP, 10, 20, 0, 8, 2, 0, 'ok', CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO audit_events ("
            "id, event_key, event_type, payload_hash, payload, retention_class, "
            "occurred_at, expires_at, reservation_id) VALUES ("
            "'audit-1', 'reservation-1:settled', 'provider_attempt_settled', ?, '{}', "
            "'operational', CURRENT_TIMESTAMP, datetime('now', '+90 days'), 'reservation-1')",
            ("c" * 64,),
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE provider_call_reservations SET run_scope_id = NULL "
                "WHERE id = 'reservation-1'"
            )
        for statement in (
            "DELETE FROM governance_policies WHERE id = 'policy-1'",
            "DELETE FROM governance_scopes WHERE id = 'scope-global'",
            "DELETE FROM evaluation_runs WHERE id = 'run-1'",
            "DELETE FROM provider_call_reservations WHERE id = 'reservation-1'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    failed = _invoke_alembic(database_path, "downgrade", WEB_CREDENTIAL_REVISION)

    assert failed.returncode != 0
    assert "ledger, audit, policy, scope" in failed.stderr
    assert _read_heads(database_path) == (GOVERNANCE_REVISION,)


def test_reliability_downgrade_rejects_active_runs_before_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "active-downgrade.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE evaluation_runs SET status = 'pending' WHERE id = 'run-1'")

    failed = _invoke_alembic(database_path, "downgrade", PHASE_1_REVISION)

    assert failed.returncode != 0
    assert "drain, cancel, or fail active runs first" in failed.stderr
    assert _read_heads(database_path) == (RELIABILITY_REVISION,)
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(evaluation_runs)")}
        assert "lease_token" in columns
        connection.execute("UPDATE evaluation_runs SET status = 'completed' WHERE id = 'run-1'")

    _run_alembic(database_path, "downgrade", PHASE_1_REVISION)
    assert _read_heads(database_path) == (PHASE_1_REVISION,)


def test_reliability_metadata_round_trip_preserves_core_evidence(tmp_path: Path) -> None:
    database_path = tmp_path / "reliability-round-trip.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        core_before = connection.execute(
            "SELECT id, status, protocol_version, total_questions, completed_questions, "
            "correct_questions, score, model_parameters_snapshot "
            "FROM evaluation_runs WHERE id = 'run-1'"
        ).fetchone()
        response_before = connection.execute(
            "SELECT id, run_id, question_id, reference_answer_snapshot, score, evaluator_name "
            "FROM evaluation_responses WHERE id = 'response-1'"
        ).fetchone()

    _run_alembic(database_path, "downgrade", PHASE_1_REVISION)
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(evaluation_runs)")}
        assert "lease_token" not in columns
        assert (
            connection.execute(
                "SELECT id, status, protocol_version, total_questions, completed_questions, "
                "correct_questions, score, model_parameters_snapshot "
                "FROM evaluation_runs WHERE id = 'run-1'"
            ).fetchone()
            == core_before
        )
        assert (
            connection.execute(
                "SELECT id, run_id, question_id, reference_answer_snapshot, score, "
                "evaluator_name FROM evaluation_responses WHERE id = 'response-1'"
            ).fetchone()
            == response_before
        )

    _run_alembic(database_path, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        reliability = connection.execute(
            "SELECT attempt_count, max_attempts, lease_token FROM evaluation_runs "
            "WHERE id = 'run-1'"
        ).fetchone()
    assert reliability == (0, 3, 0)
    _run_alembic(database_path, "check")


def test_reliability_constraints_reject_incoherent_lease_state(tmp_path: Path) -> None:
    database_path = tmp_path / "reliability-constraints.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    _insert_legacy_rows(database_path)
    _run_alembic(database_path, "upgrade", "head")

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE evaluation_runs SET attempt_count = -1 WHERE id = 'run-1'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE evaluation_runs SET status = 'running' WHERE id = 'run-1'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE evaluation_runs SET next_attempt_at = CURRENT_TIMESTAMP WHERE id = 'run-1'"
            )


def test_prepare_is_idempotent_for_versioned_database(tmp_path: Path) -> None:
    database_path = tmp_path / "versioned.db"
    _run_alembic(database_path, "upgrade", "head")

    result = prepare_database(_database_url(database_path))

    assert result.action == "versioned"
    assert result.backup_path is None
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_partial_schema_without_stamping(tmp_path: Path) -> None:
    database_path = tmp_path / "partial.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE models (id TEXT PRIMARY KEY)")

    with pytest.raises(SchemaPreparationError, match="only part"):
        prepare_database(_database_url(database_path))

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "alembic_version" not in tables
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_server_default_drift_without_stamping(tmp_path: Path) -> None:
    database_path = tmp_path / "default-drift.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()
    _rewrite_models_ddl(
        database_path,
        "enabled BOOLEAN NOT NULL",
        "enabled BOOLEAN DEFAULT 1 NOT NULL",
    )

    with pytest.raises(SchemaPreparationError, match="does not match"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == ()
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_check_constraint_drift_without_stamping(tmp_path: Path) -> None:
    database_path = tmp_path / "constraint-drift.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()
    _rewrite_models_ddl(
        database_path,
        "input_price_per_million >= 0",
        "input_price_per_million > 0",
    )

    with pytest.raises(SchemaPreparationError, match="Check constraints"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == ()
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_duplicate_named_check_without_stamping(tmp_path: Path) -> None:
    database_path = tmp_path / "duplicate-check.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()
    original_check = (
        "CONSTRAINT ck_models_input_price_nonnegative CHECK (input_price_per_million >= 0)"
    )
    _rewrite_models_ddl(
        database_path,
        original_check,
        original_check + ", CONSTRAINT ck_models_input_price_nonnegative "
        "CHECK (input_price_per_million IS NULL OR input_price_per_million >= 100)",
    )

    with pytest.raises(SchemaPreparationError, match="Check constraints"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == ()
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_duplicate_unique_constraint_without_stamping(tmp_path: Path) -> None:
    database_path = tmp_path / "duplicate-unique.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()
    unique = "CONSTRAINT uq_models_name UNIQUE (name)"
    _rewrite_models_ddl(database_path, unique, f"{unique}, {unique}")

    with pytest.raises(SchemaPreparationError, match="Keys or indexes"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == ()
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_duplicate_foreign_key_without_stamping(tmp_path: Path) -> None:
    database_path = tmp_path / "duplicate-foreign-key.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()
    foreign_key = (
        "CONSTRAINT fk_questions_benchmark_id_benchmarks FOREIGN KEY(benchmark_id) "
        "REFERENCES benchmarks (id) ON DELETE CASCADE"
    )
    _rewrite_table_ddl(
        database_path,
        "questions",
        foreign_key,
        f"{foreign_key}, {foreign_key}",
    )

    with pytest.raises(SchemaPreparationError, match="Keys or indexes"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == ()
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_conflict_policy_without_stamping(tmp_path: Path) -> None:
    database_path = tmp_path / "conflict-policy.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()
    _rewrite_models_ddl(
        database_path,
        "CONSTRAINT uq_models_name UNIQUE (name)",
        "CONSTRAINT uq_models_name UNIQUE (name) ON CONFLICT IGNORE",
    )

    with pytest.raises(SchemaPreparationError, match="DDL modifiers"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == ()
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_drift_in_versioned_head(tmp_path: Path) -> None:
    database_path = tmp_path / "versioned-head-drift.db"
    _run_alembic(database_path, "upgrade", "head")
    _rewrite_models_ddl(
        database_path,
        "CONSTRAINT uq_models_name UNIQUE (name)",
        "CONSTRAINT uq_models_name UNIQUE (name) ON CONFLICT IGNORE",
    )

    with pytest.raises(SchemaPreparationError, match="DDL modifiers"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == (HEAD_REVISION,)
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_shorthand_generated_column_without_stamping(tmp_path: Path) -> None:
    database_path = tmp_path / "generated-column.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()
    _rewrite_models_ddl(
        database_path,
        "base_url VARCHAR(2048)",
        "base_url VARCHAR(2048) AS (NULL) VIRTUAL",
    )

    with pytest.raises(SchemaPreparationError, match="generated columns"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == ()
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_partial_index_without_stamping(tmp_path: Path) -> None:
    database_path = tmp_path / "partial-index.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX ix_models_enabled")
            connection.exec_driver_sql(
                "CREATE INDEX ix_models_enabled ON models (enabled) WHERE enabled = 1"
            )
    finally:
        engine.dispose()

    with pytest.raises(SchemaPreparationError, match="DDL modifiers"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == ()
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_active_policy_partial_index_predicate_drift(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "active-policy-index-drift.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX uq_governance_policies_single_active")
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_governance_policies_single_active "
                "ON governance_policies (is_active) WHERE is_active = 0"
            )
    finally:
        engine.dispose()

    with pytest.raises(SchemaPreparationError, match="Keys or indexes"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == ()
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_sqlite_trigger_without_stamping(tmp_path: Path) -> None:
    database_path = tmp_path / "trigger-drift.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TRIGGER block_models BEFORE INSERT ON models "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
    finally:
        engine.dispose()

    with pytest.raises(SchemaPreparationError, match="triggers"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == ()
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_missing_primary_key_without_stamping(tmp_path: Path) -> None:
    database_path = tmp_path / "primary-key-drift.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()
    _rebuild_responses_without_primary_key(database_path)

    with pytest.raises(SchemaPreparationError, match="Keys or indexes"):
        prepare_database(_database_url(database_path))

    assert _read_heads(database_path) == ()
    assert list(tmp_path.glob("*.bak")) == []


def test_prepare_rejects_without_rowid_legacy_table_before_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "without-rowid-legacy.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM alembic_version")
    _rebuild_questions_without_rowid(database_path)

    with pytest.raises(SchemaPreparationError, match="WITHOUT ROWID"):
        prepare_database(_database_url(database_path))
    failed_upgrade = _invoke_alembic(database_path, "stamp", LEGACY_REVISION)
    assert failed_upgrade.returncode == 0
    failed_upgrade = _invoke_alembic(database_path, "upgrade", "head")

    assert failed_upgrade.returncode != 0
    assert "must support SQLite rowid" in failed_upgrade.stderr
    assert _read_heads(database_path) == (LEGACY_REVISION,)
    with sqlite3.connect(database_path) as connection:
        temporary_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '_alembic_tmp_%'"
        ).fetchall()
    assert temporary_tables == []
    assert list(tmp_path.glob("*.bak")) == []


def test_versioned_legacy_invalid_rows_fail_before_batch_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "invalid-versioned-legacy.db"
    _run_alembic(database_path, "upgrade", LEGACY_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO models (
                id, name, provider_type, base_url, remote_model_name, api_key_env,
                enabled, input_price_per_million, output_price_per_million,
                default_parameters, created_at, updated_at
            ) VALUES (
                'invalid-model', 'Invalid Mock', 'mock', 'https://invalid.example', NULL, NULL,
                1, 0, 0, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            """
        )

    with pytest.raises(SchemaPreparationError, match="provider configuration"):
        prepare_database(_database_url(database_path))
    failed_upgrade = _invoke_alembic(database_path, "upgrade", "head")

    assert failed_upgrade.returncode != 0
    assert "correct the rows before upgrading" in failed_upgrade.stderr
    assert _read_heads(database_path) == (LEGACY_REVISION,)
    with sqlite3.connect(database_path) as connection:
        temporary_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '_alembic_tmp_%'"
        ).fetchall()
    assert temporary_tables == []
    assert list(tmp_path.glob("*.bak")) == []


def test_startup_schema_guard_requires_head(tmp_path: Path) -> None:
    database_path = tmp_path / "startup.db"
    engine = create_database_engine(_database_url(database_path))
    try:
        Base.metadata.create_all(bind=engine)
        with pytest.raises(RuntimeError, match="make setup"):
            require_database_at_head(engine)
        stamp_database(engine, "head")
        require_database_at_head(engine)
    finally:
        engine.dispose()
