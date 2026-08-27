"""Evidence for the explicit, atomic SQLite-to-PostgreSQL importer."""

from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.engine import make_url

import app.db.import_sqlite as import_sqlite_module
from app.core.constants import PROTOCOL_VERSION
from app.db.base import Base
from app.db.import_sqlite import (
    CORE_TABLE_NAMES,
    SQLiteImportCommitOutcomeUnknownError,
    SQLiteImportCommittedVerificationError,
    SQLiteImportError,
    _postgresql_url,
    _read_only_sqlite_engine,
    _require_database_head,
    _require_empty_target,
    _sqlite_path,
    canonical_table_summary,
    copy_snapshot,
    import_sqlite_to_postgres,
    main,
    preflight_sqlite_source,
    snapshot_database,
)
from app.db.session import create_database_engine
from app.db.types import UTCDateTime
from app.models import CredentialSource, ProviderType, QuestionType, RunStatus
from app.security import CredentialKeyring, EncryptedCredential

BACKEND_ROOT = Path(__file__).resolve().parents[1]
IMPORT_CREDENTIAL_CANARY = "sk-import-canary-Q8mT3vN7rL2pX5cK9wS4"
IMPORT_KEY_ID = "import-test-key-v1"
IMPORT_KEY_MATERIAL = bytes(range(32))


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


def _run_alembic_url(database_url: str, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["LLMBENCHLAB_DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run_alembic(path: Path, revision: str = "head") -> None:
    _run_alembic_url(_sqlite_url(path), "upgrade", revision)


def _insert_complete_mock_evidence(
    path: Path,
    *,
    status: RunStatus = RunStatus.COMPLETED,
    namespace: str = "",
) -> EncryptedCredential:
    created_at = datetime(2026, 8, 25, 4, 5, 6, 123456, tzinfo=UTC)
    finished_at = created_at + timedelta(seconds=2)
    suffix = f"-{namespace}" if namespace else ""
    model_id = f"model-import{suffix}"
    stored_model_id = f"model-credential-import{suffix}"
    benchmark_id = f"benchmark-import{suffix}"
    run_id = f"run-import{suffix}"
    question_ids = (f"question-import-1{suffix}", f"question-import-2{suffix}")
    engine = create_database_engine(_sqlite_url(path))
    tables = Base.metadata.tables
    encrypted = CredentialKeyring(
        IMPORT_KEY_ID,
        {IMPORT_KEY_ID: IMPORT_KEY_MATERIAL},
    ).encrypt(
        SecretStr(IMPORT_CREDENTIAL_CANARY),
        model_id=stored_model_id,
        provider_base_url="https://provider.example/v1",
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                tables["models"].insert(),
                {
                    "id": model_id,
                    "name": f"SQLite Import Mock {namespace}".rstrip(),
                    "provider_type": ProviderType.MOCK,
                    "base_url": None,
                    "remote_model_name": None,
                    "api_key_env": None,
                    "enabled": True,
                    "input_price_per_million": Decimal("1.25000000"),
                    "output_price_per_million": Decimal("2.50000000"),
                    "default_parameters": {
                        "temperature": 0,
                        "nested": {"beta": [True, None], "alpha": "value"},
                    },
                    "created_at": created_at,
                    "updated_at": finished_at,
                },
            )
            connection.execute(
                tables["models"].insert(),
                {
                    "id": stored_model_id,
                    "name": f"Encrypted Import Provider {namespace}".rstrip(),
                    "provider_type": ProviderType.OPENAI_COMPATIBLE,
                    "base_url": "https://provider.example/v1",
                    "remote_model_name": "provider-model",
                    "api_key_env": None,
                    "credential_source": CredentialSource.STORED,
                    "enabled": True,
                    "input_price_per_million": None,
                    "output_price_per_million": None,
                    "default_parameters": {},
                    "created_at": created_at,
                    "updated_at": finished_at,
                },
            )
            connection.execute(
                tables["model_credentials"].insert(),
                {
                    "model_id": stored_model_id,
                    "algorithm": encrypted.algorithm,
                    "key_id": encrypted.key_id,
                    "nonce": encrypted.nonce,
                    "ciphertext": encrypted.ciphertext,
                    "created_at": created_at,
                    "updated_at": finished_at,
                },
            )
            connection.execute(
                tables["benchmarks"].insert(),
                {
                    "id": benchmark_id,
                    "slug": f"sqlite-import-mock{suffix}",
                    "name": "SQLite import fixture",
                    "version": "1.0.0",
                    "description": "Complete Mock evidence fixture",
                    "dimension": "general",
                    "language": "en",
                    "license": "MIT",
                    "source": "local-test",
                    "evaluator_type": "exact_match",
                    "evaluator_config": {"trim": True},
                    "prompt_template": {"system": "Answer exactly", "suffix": ""},
                    "schema_version": "llmbenchlab-dataset-v1",
                    "dataset_hash": "a" * 64,
                    "question_count": 2,
                    "is_demo": False,
                    "created_at": created_at,
                },
            )
            connection.execute(
                tables["questions"].insert(),
                [
                    {
                        "id": question_ids[0],
                        "benchmark_id": benchmark_id,
                        "external_id": "q1",
                        "position": 0,
                        "question_type": QuestionType.EXACT_MATCH,
                        "prompt": "One?",
                        "choices": None,
                        "reference_answer": "one",
                        "evaluator_config": {"case_sensitive": False},
                        "metadata": {"difficulty": 1, "tags": ["mock", "import"]},
                    },
                    {
                        "id": question_ids[1],
                        "benchmark_id": benchmark_id,
                        "external_id": "q2",
                        "position": 1,
                        "question_type": QuestionType.MULTIPLE_CHOICE,
                        "prompt": "Choose A?",
                        "choices": {"B": "No", "A": "Yes"},
                        "reference_answer": "A",
                        "evaluator_config": {},
                        "metadata": {"difficulty": 2},
                    },
                ],
            )
            completed = 2 if status == RunStatus.COMPLETED else 0
            connection.execute(
                tables["evaluation_runs"].insert(),
                {
                    "id": run_id,
                    "model_id": model_id,
                    "benchmark_id": benchmark_id,
                    "status": status,
                    "protocol_version": PROTOCOL_VERSION,
                    "model_parameters_snapshot": {
                        "temperature": 0,
                        "execution": {
                            "restart_recovery": "database_lease_resume_missing_responses"
                        },
                    },
                    "benchmark_hash_snapshot": "a" * 64,
                    "prompt_template_snapshot": {"system": "Answer exactly", "suffix": ""},
                    "code_commit_sha": "b" * 40,
                    "total_questions": 2,
                    "completed_questions": completed,
                    "correct_questions": 1 if completed else 0,
                    "error_questions": 0,
                    "score": 50.0 if completed else None,
                    "completion_rate": 100.0 if completed else None,
                    "answered_accuracy": 50.0 if completed else None,
                    "average_latency_ms": 12.75 if completed else None,
                    "input_tokens": 17 if completed else None,
                    "output_tokens": 7 if completed else None,
                    "estimated_cost": Decimal("0.00000123") if completed else None,
                    "cancellation_requested": False,
                    "attempt_count": 2,
                    "max_attempts": 4,
                    "lease_owner": "worker-import" if status == RunStatus.RUNNING else None,
                    "lease_token": 7,
                    "lease_expires_at": (
                        finished_at + timedelta(seconds=30) if status == RunStatus.RUNNING else None
                    ),
                    "heartbeat_at": created_at if status == RunStatus.RUNNING else None,
                    "next_attempt_at": None,
                    "last_enqueued_at": created_at,
                    "last_error": None,
                    "dead_lettered_at": None,
                    "started_at": created_at if status != RunStatus.PENDING else None,
                    "finished_at": finished_at if completed else None,
                    "created_at": created_at,
                    "error_message": None,
                },
            )
            if completed:
                connection.execute(
                    tables["evaluation_responses"].insert(),
                    [
                        {
                            "id": f"response-import-1{suffix}",
                            "run_id": run_id,
                            "question_id": question_ids[0],
                            "raw_response": "one",
                            "parsed_answer": "one",
                            "reference_answer_snapshot": "one",
                            "score": 1.0,
                            "evaluator_name": "exact_match_v1",
                            "latency_ms": 12.5,
                            "input_tokens": 8,
                            "output_tokens": 3,
                            "estimated_cost": Decimal("0.00000050"),
                            "error_type": None,
                            "error_message": None,
                            "created_at": created_at + timedelta(seconds=1),
                        },
                        {
                            "id": f"response-import-2{suffix}",
                            "run_id": run_id,
                            "question_id": question_ids[1],
                            "raw_response": "B",
                            "parsed_answer": {"choice": "B", "confidence": 0.25},
                            "reference_answer_snapshot": "A",
                            "score": 0.0,
                            "evaluator_name": "multiple_choice_v1",
                            "latency_ms": 13.0,
                            "input_tokens": 9,
                            "output_tokens": 4,
                            "estimated_cost": Decimal("0.00000073"),
                            "error_type": None,
                            "error_message": None,
                            "created_at": finished_at,
                        },
                    ],
                )
    finally:
        engine.dispose()
    return encrypted


def _read_only_snapshot(path: Path):
    engine = _read_only_sqlite_engine(path)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            preflight_sqlite_source(connection)
            return snapshot_database(connection)
    finally:
        engine.dispose()


def _core_counts(engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            name: connection.scalar(
                sa.select(sa.func.count()).select_from(Base.metadata.tables[name])
            )
            for name in CORE_TABLE_NAMES
        }


def _truncate_postgres(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE evaluation_responses, evaluation_runs, questions, benchmarks, models "
            "RESTART IDENTITY CASCADE"
        )


def test_canonical_summary_is_order_independent_and_cross_dialect_stable() -> None:
    metadata = sa.MetaData()
    table = sa.Table(
        "canonical_fixture",
        metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("payload", sa.JSON()),
        sa.Column("amount", sa.Numeric(20, 8)),
        sa.Column("ratio", sa.Float()),
        sa.Column("created_at", UTCDateTime()),
    )
    instant = datetime(2026, 8, 25, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    rows = [
        {
            "id": "b",
            "payload": {"z": [2, 1], "a": {"value": True}},
            "amount": Decimal("1.23000000"),
            "ratio": 0.25,
            "created_at": instant,
        },
        {
            "id": "a",
            "payload": {"value": None},
            "amount": Decimal("0E-8"),
            "ratio": -0.0,
            "created_at": instant,
        },
    ]
    equivalent = [
        {
            **rows[1],
            "amount": Decimal("0.00000000"),
            "ratio": 0.0,
            "created_at": instant.astimezone(UTC),
        },
        {
            **rows[0],
            "payload": {"a": {"value": True}, "z": [2, 1]},
            "amount": Decimal("1.23"),
            "created_at": instant.astimezone(UTC),
        },
    ]

    summary = canonical_table_summary(table, rows)
    assert summary == canonical_table_summary(table, equivalent)

    changed = [{**row} for row in equivalent]
    changed[0]["payload"] = {"value": "changed"}
    changed_summary = canonical_table_summary(table, changed)
    assert changed_summary.row_count == summary.row_count
    assert changed_summary.pk_set_digest == summary.pk_set_digest
    assert changed_summary.canonical_row_digest != summary.canonical_row_digest


def test_url_preflight_rejects_wrong_dialects_and_missing_source(tmp_path: Path) -> None:
    with pytest.raises(SQLiteImportError, match="Source database must use SQLite"):
        _sqlite_path("postgresql+psycopg://user:secret@localhost/database")
    with pytest.raises(SQLiteImportError, match="Target database must use PostgreSQL"):
        _postgresql_url(f"sqlite:///{tmp_path / 'target.db'}")
    with pytest.raises(SQLiteImportError, match="does not exist"):
        _sqlite_path(str(tmp_path / "missing.db"))


@pytest.mark.parametrize(
    "target",
    [
        "postgresql+psycopg://user:must-not-appear@localhost/database",
        "postgresql+psycopg://user@localhost/database?password=must-not-appear",
    ],
)
def test_cli_rejects_password_in_argv_without_echoing_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    target: str,
) -> None:
    secret = "must-not-appear"
    result = main(
        [
            "--source",
            str(tmp_path / "missing.db"),
            "--target",
            target,
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "--target must not contain a password" in captured.err
    assert secret not in captured.out + captured.err


def test_cli_distinguishes_committed_verification_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def committed_failure(*_args, **_kwargs):
        raise SQLiteImportCommittedVerificationError("safe test detail")

    monkeypatch.setattr(
        import_sqlite_module,
        "import_sqlite_to_postgres",
        committed_failure,
    )

    result = main(
        [
            "--source",
            str(tmp_path / "source.db"),
            "--target",
            "postgresql+psycopg://llmbenchlab@localhost/empty_target",
        ]
    )

    captured = capsys.readouterr()
    assert result == 3
    assert "status=committed_but_verification_failed" in captured.err
    assert "do not retry blindly" in captured.err
    assert "safe test detail" not in captured.out + captured.err


def test_cli_distinguishes_commit_outcome_unknown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def commit_unknown(*_args, **_kwargs):
        raise SQLiteImportCommitOutcomeUnknownError("safe test detail")

    monkeypatch.setattr(
        import_sqlite_module,
        "import_sqlite_to_postgres",
        commit_unknown,
    )

    result = main(
        [
            "--source",
            str(tmp_path / "source.db"),
            "--target",
            "postgresql+psycopg://llmbenchlab@localhost/empty_target",
        ]
    )

    captured = capsys.readouterr()
    assert result == 4
    assert "status=commit_outcome_unknown" in captured.err
    assert "may be empty or fully populated" in captured.err
    assert "do not retry blindly" in captured.err
    assert "safe test detail" not in captured.out + captured.err


def test_read_only_source_preflight_preserves_file_and_reconciles_rows(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _run_alembic(source)
    _insert_complete_mock_evidence(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    before_mtime = source.stat().st_mtime_ns

    snapshot = _read_only_snapshot(source)

    assert snapshot.summaries["models"].row_count == 2
    assert snapshot.summaries["model_credentials"].row_count == 1
    assert snapshot.summaries["questions"].row_count == 2
    assert snapshot.summaries["evaluation_responses"].row_count == 2
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert source.stat().st_mtime_ns == before_mtime


def test_source_preflight_rejects_non_head_database(tmp_path: Path) -> None:
    source = tmp_path / "phase1.db"
    _run_alembic(source, "20260824_0001")
    engine = _read_only_sqlite_engine(source)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            with pytest.raises(SQLiteImportError, match="must be at Alembic head"):
                preflight_sqlite_source(connection)
    finally:
        engine.dispose()


@pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.RUNNING])
def test_source_preflight_rejects_active_runs(tmp_path: Path, status: RunStatus) -> None:
    source = tmp_path / f"active-{status.value}.db"
    _run_alembic(source)
    _insert_complete_mock_evidence(source, status=status)
    engine = _read_only_sqlite_engine(source)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            with pytest.raises(SQLiteImportError, match="pending or running"):
                preflight_sqlite_source(connection)
    finally:
        engine.dispose()


def test_source_preflight_rejects_foreign_key_damage(tmp_path: Path) -> None:
    source = tmp_path / "foreign-key-damage.db"
    _run_alembic(source)
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO questions ("
            "id, benchmark_id, external_id, position, question_type, prompt, choices, "
            "reference_answer, evaluator_config, metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "orphan-question",
                "missing-benchmark",
                "orphan",
                0,
                "exact_match",
                "orphan",
                None,
                '"answer"',
                "{}",
                "{}",
            ),
        )

    engine = _read_only_sqlite_engine(source)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            with pytest.raises(SQLiteImportError, match="foreign_key_check failed"):
                preflight_sqlite_source(connection)
    finally:
        engine.dispose()


def test_target_helpers_reject_non_head_and_nonempty_databases(tmp_path: Path) -> None:
    unversioned = create_database_engine(_sqlite_url(tmp_path / "unversioned.db"))
    try:
        with (
            unversioned.connect() as connection,
            pytest.raises(SQLiteImportError, match="must be at Alembic head"),
        ):
            _require_database_head(connection, role="Target PostgreSQL")
    finally:
        unversioned.dispose()

    target_path = tmp_path / "nonempty.db"
    _run_alembic(target_path)
    _insert_complete_mock_evidence(target_path)
    target = create_database_engine(_sqlite_url(target_path))
    try:
        with (
            target.connect() as connection,
            pytest.raises(SQLiteImportError, match="must be empty"),
        ):
            _require_empty_target(connection)
    finally:
        target.dispose()


def test_copy_failure_rolls_back_every_target_table(tmp_path: Path) -> None:
    source = tmp_path / "rollback-source.db"
    target_path = tmp_path / "rollback-target.db"
    _run_alembic(source)
    _run_alembic(target_path)
    _insert_complete_mock_evidence(source)
    source_snapshot = _read_only_snapshot(source)
    target = create_database_engine(_sqlite_url(target_path))

    def fail_on_questions(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO QUESTIONS"):
            raise RuntimeError("injected copy failure")

    sa.event.listen(target, "before_cursor_execute", fail_on_questions)
    try:
        with (
            pytest.raises(RuntimeError, match="injected copy failure"),
            target.begin() as connection,
        ):
            copy_snapshot(source_snapshot, connection)
    finally:
        sa.event.remove(target, "before_cursor_execute", fail_on_questions)

    try:
        assert _core_counts(target) == dict.fromkeys(CORE_TABLE_NAMES, 0)
    finally:
        target.dispose()


def test_copy_snapshot_preserves_all_six_tables_in_sqlite(tmp_path: Path) -> None:
    source = tmp_path / "copy-source.db"
    target_path = tmp_path / "copy-target.db"
    _run_alembic(source)
    _run_alembic(target_path)
    _insert_complete_mock_evidence(source)
    source_snapshot = _read_only_snapshot(source)
    target = create_database_engine(_sqlite_url(target_path))
    try:
        with target.begin() as connection:
            copy_snapshot(source_snapshot, connection)
            precommit = snapshot_database(connection)
        with target.connect() as connection:
            postcommit = snapshot_database(connection)
    finally:
        target.dispose()

    assert precommit.summaries == source_snapshot.summaries
    assert postcommit.summaries == source_snapshot.summaries
    assert postcommit.rows["questions"][0]["metadata"] == {
        "difficulty": 1,
        "tags": ["mock", "import"],
    }
    assert postcommit.rows["evaluation_runs"][0]["protocol_version"] == PROTOCOL_VERSION
    assert postcommit.rows["evaluation_runs"][0]["attempt_count"] == 2
    assert postcommit.rows["evaluation_responses"][1]["estimated_cost"] == Decimal("0.00000073")
    assert len(postcommit.rows["model_credentials"]) == 1
    assert postcommit.rows["model_credentials"][0] == source_snapshot.rows["model_credentials"][0]
    assert IMPORT_CREDENTIAL_CANARY.encode() not in target_path.read_bytes()


@pytest.mark.integration
def test_real_postgres_import_preserves_complete_mock_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    management_url = os.environ.get("LLMBENCHLAB_TEST_POSTGRES_URL")
    if not management_url:
        pytest.skip("LLMBENCHLAB_TEST_POSTGRES_URL is required")
    parsed_management_url = make_url(management_url)
    assert parsed_management_url.get_backend_name() == "postgresql"
    management_database = parsed_management_url.database
    if parsed_management_url.host not in {"127.0.0.1", "::1", "localhost"}:
        pytest.fail("PostgreSQL import integration only creates databases on loopback hosts")
    if not management_database:
        pytest.fail("PostgreSQL integration management URL must name a database")

    target_database = f"llmbenchlab_import_{uuid4().hex}_test"
    assert re.fullmatch(r"llmbenchlab_import_[a-f0-9]{32}_test", target_database)
    target_url = parsed_management_url.set(database=target_database).render_as_string(
        hide_password=False
    )

    source = tmp_path / "complete-mock.db"
    rival_source = tmp_path / "complete-mock-rival.db"
    _run_alembic(source)
    _run_alembic(rival_source)
    credential_evidence = _insert_complete_mock_evidence(source)
    _insert_complete_mock_evidence(rival_source, namespace="rival")
    source_file_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    management = create_database_engine(management_url)
    target = None
    database_created = False
    try:
        with management.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            actual_management_database = connection.exec_driver_sql(
                "SELECT current_database()"
            ).scalar_one()
            if actual_management_database != management_database:
                pytest.fail(
                    "Connected PostgreSQL management database does not match its explicit URL"
                )
            connection.exec_driver_sql(f'CREATE DATABASE "{target_database}"')
            database_created = True

        _run_alembic_url(target_url, "upgrade", "head")
        _run_alembic_url(target_url, "check")
        target = create_database_engine(target_url)
        with target.connect() as connection:
            _require_database_head(connection, role="Target PostgreSQL")
            actual_database = connection.exec_driver_sql("SELECT current_database()").scalar_one()
        if actual_database != target_database:
            pytest.fail("Connected PostgreSQL database is not the dedicated import test target")

        def fail_after_parent_tables(snapshot, connection) -> None:
            for table_name in CORE_TABLE_NAMES[:2]:
                table = Base.metadata.tables[table_name]
                connection.execute(
                    table.insert(),
                    [dict(row) for row in snapshot.rows[table_name]],
                )
            raise RuntimeError("injected PostgreSQL copy failure")

        with monkeypatch.context() as failure_patch:
            failure_patch.setattr(
                import_sqlite_module,
                "copy_snapshot",
                fail_after_parent_tables,
            )
            with pytest.raises(RuntimeError, match="injected PostgreSQL copy failure"):
                import_sqlite_to_postgres(str(source), target_url, output=io.StringIO())
        assert _core_counts(target) == dict.fromkeys(CORE_TABLE_NAMES, 0)

        barrier = Barrier(2)

        def compete(label: str, candidate: Path) -> tuple[str, str, str]:
            barrier.wait(timeout=10)
            try:
                import_sqlite_to_postgres(
                    str(candidate),
                    target_url,
                    output=io.StringIO(),
                )
            except SQLiteImportError as exc:
                return ("rejected", label, str(exc))
            return ("imported", label, "")

        candidates = {"primary": source, "rival": rival_source}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                label: executor.submit(compete, label, candidate)
                for label, candidate in candidates.items()
            }
            outcomes = [future.result(timeout=20) for future in futures.values()]
        imported = [outcome for outcome in outcomes if outcome[0] == "imported"]
        rejected = [outcome for outcome in outcomes if outcome[0] == "rejected"]
        assert len(imported) == len(rejected) == 1
        assert "must be empty" in rejected[0][2]
        winning_source = candidates[imported[0][1]]
        with target.connect() as connection:
            concurrent_target = snapshot_database(connection)
        assert concurrent_target.summaries == _read_only_snapshot(winning_source).summaries

        _truncate_postgres(target)
        real_connection_begin = sa.engine.Connection.begin

        class CommitAcknowledgementLost:
            def __init__(self, transaction) -> None:
                self.transaction = transaction

            def commit(self) -> None:
                self.transaction.commit()
                raise RuntimeError("injected COMMIT acknowledgement loss")

            def rollback(self) -> None:
                self.transaction.rollback()

        def begin_with_lost_commit_ack(connection):
            transaction = real_connection_begin(connection)
            if connection.dialect.name == "postgresql":
                return CommitAcknowledgementLost(transaction)
            return transaction

        with monkeypatch.context() as commit_patch:
            commit_patch.setattr(
                sa.engine.Connection,
                "begin",
                begin_with_lost_commit_ack,
            )
            with pytest.raises(
                SQLiteImportCommitOutcomeUnknownError,
                match="did not confirm COMMIT",
            ):
                import_sqlite_to_postgres(str(source), target_url, output=io.StringIO())
        with target.connect() as connection:
            committed_after_ack_loss = snapshot_database(connection)
        assert committed_after_ack_loss.summaries == _read_only_snapshot(source).summaries
        with pytest.raises(SQLiteImportError, match="must be empty"):
            import_sqlite_to_postgres(str(source), target_url, output=io.StringIO())

        _truncate_postgres(target)
        snapshot_calls = 0
        postcommit_transaction: list[tuple[str, str]] = []
        real_snapshot_database = import_sqlite_module.snapshot_database

        def fail_postcommit_snapshot(connection):
            nonlocal snapshot_calls
            snapshot_calls += 1
            if snapshot_calls == 3:
                postcommit_transaction.append(
                    (
                        connection.exec_driver_sql("SHOW transaction_isolation").scalar_one(),
                        connection.exec_driver_sql("SHOW transaction_read_only").scalar_one(),
                    )
                )
                raise RuntimeError("injected post-commit snapshot failure")
            return real_snapshot_database(connection)

        with monkeypatch.context() as postcommit_patch:
            postcommit_patch.setattr(
                import_sqlite_module,
                "snapshot_database",
                fail_postcommit_snapshot,
            )
            with pytest.raises(
                SQLiteImportCommittedVerificationError,
                match="Target transaction committed",
            ):
                import_sqlite_to_postgres(str(source), target_url, output=io.StringIO())
        assert postcommit_transaction == [("repeatable read", "on")]
        with target.connect() as connection:
            committed_after_snapshot_failure = snapshot_database(connection)
        assert committed_after_snapshot_failure.summaries == _read_only_snapshot(source).summaries
        with pytest.raises(SQLiteImportError, match="must be empty"):
            import_sqlite_to_postgres(str(source), target_url, output=io.StringIO())

        _truncate_postgres(target)

        class FailOnPostcommitOutput(io.StringIO):
            def write(self, value: str) -> int:
                if "phase=postcommit_target" in value:
                    raise OSError("injected post-commit output failure")
                return super().write(value)

        with pytest.raises(
            SQLiteImportCommittedVerificationError,
            match="Target transaction committed",
        ):
            import_sqlite_to_postgres(
                str(source),
                target_url,
                output=FailOnPostcommitOutput(),
            )
        with target.connect() as connection:
            committed_after_output_failure = snapshot_database(connection)
        assert committed_after_output_failure.summaries == _read_only_snapshot(source).summaries

        _truncate_postgres(target)
        output = io.StringIO()
        report = import_sqlite_to_postgres(str(source), target_url, output=output)

        assert report.source == report.precommit_target == report.postcommit_target
        assert report.source["models"].row_count == 2
        assert report.source["model_credentials"].row_count == 1
        assert report.source["benchmarks"].row_count == 1
        assert report.source["questions"].row_count == 2
        assert report.source["evaluation_runs"].row_count == 1
        assert report.source["evaluation_responses"].row_count == 2
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_file_digest

        source_summary = _read_only_snapshot(source).summaries
        with target.connect() as connection:
            target_snapshot = snapshot_database(connection)
            run = (
                connection.execute(
                    sa.select(Base.metadata.tables["evaluation_runs"]).where(
                        Base.metadata.tables["evaluation_runs"].c.id == "run-import"
                    )
                )
                .mappings()
                .one()
            )
            responses = (
                connection.execute(
                    sa.select(Base.metadata.tables["evaluation_responses"]).order_by(
                        Base.metadata.tables["evaluation_responses"].c.id
                    )
                )
                .mappings()
                .all()
            )

        assert target_snapshot.summaries == source_summary
        assert run["protocol_version"] == PROTOCOL_VERSION
        assert run["attempt_count"] == 2
        assert run["max_attempts"] == 4
        assert run["lease_token"] == 7
        assert run["model_parameters_snapshot"]["execution"]["restart_recovery"] == (
            "database_lease_resume_missing_responses"
        )
        assert run["estimated_cost"] == Decimal("0.00000123")
        assert [response["id"] for response in responses] == [
            "response-import-1",
            "response-import-2",
        ]
        assert responses[1]["parsed_answer"] == {"choice": "B", "confidence": 0.25}

        with pytest.raises(SQLiteImportError, match="must be empty"):
            import_sqlite_to_postgres(str(source), target_url, output=io.StringIO())
        with target.connect() as connection:
            after_rejected_repeat = snapshot_database(connection)
        assert after_rejected_repeat.summaries == target_snapshot.summaries

        report_lines = output.getvalue().splitlines()
        assert len(report_lines) == len(CORE_TABLE_NAMES) * 3
        assert all("row_count=" in line for line in report_lines)
        assert all("pk_set_digest=sha256:" in line for line in report_lines)
        assert all("canonical_row_digest=sha256:" in line for line in report_lines)
        assert "SQLite Import Mock" not in output.getvalue()
        assert "One?" not in output.getvalue()
        forbidden_credential_output = (
            IMPORT_CREDENTIAL_CANARY,
            IMPORT_KEY_ID,
            credential_evidence.nonce.hex(),
            credential_evidence.ciphertext.hex(),
        )
        assert all(marker not in output.getvalue() for marker in forbidden_credential_output)
        assert IMPORT_CREDENTIAL_CANARY.encode() not in source.read_bytes()

        _truncate_postgres(target)
        target_env = "LLMBENCHLAB_IMPORT_TEST_TARGET_URL"
        monkeypatch.setenv(target_env, target_url)
        assert main(["--source", _sqlite_url(source), "--target-env", target_env]) == 0
        cli_output = capsys.readouterr().out
        assert "SQLite to PostgreSQL import completed and reconciled" in cli_output
        assert "SQLite Import Mock" not in cli_output
        assert "One?" not in cli_output
        assert all(marker not in cli_output for marker in forbidden_credential_output)
        if parsed_management_url.password:
            assert parsed_management_url.password not in cli_output
        with target.connect() as connection:
            cli_snapshot = snapshot_database(connection)
        assert cli_snapshot.summaries == source_summary
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_file_digest
    finally:
        if target is not None:
            target.dispose()
        if database_created:
            with management.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{target_database}" WITH (FORCE)')
        management.dispose()
