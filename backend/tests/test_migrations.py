"""Regression tests for clean migrations and safe legacy SQLite adoption."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.db.base import Base
from app.db.prepare_migrations import (
    LEGACY_REVISION,
    PHASE_1_REVISION,
    SchemaPreparationError,
    database_heads,
    prepare_database,
    require_database_at_head,
    stamp_database,
)
from app.db.session import create_database_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
HEAD_REVISION = "20260827_0003"
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

    assert result.action == "stamped_current"
    assert result.stamped_revision == HEAD_REVISION
    assert result.backup_path is not None and result.backup_path.is_file()
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
    assert _read_heads(database_path) == (HEAD_REVISION,)
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
