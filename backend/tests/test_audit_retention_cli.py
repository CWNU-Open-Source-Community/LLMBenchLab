"""Fixed, secret-minimized audit retention CLI behavior."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.cli import audit_retention
from app.governance.audit_archive import write_archive


def test_verify_is_offline_and_does_not_print_archive_path(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "secret-internal-name.jsonl"
    written = write_archive(
        path,
        (),
        cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_alembic_head="20260828_0005",
        has_more_eligible=False,
    )

    def reject_database_access():
        raise AssertionError("verify must not create a database factory")

    monkeypatch.setattr(audit_retention, "_database_factory", reject_database_access)
    status = audit_retention.main(
        ["verify", "--archive", str(path), "--expected-sha256", written.archive_sha256]
    )
    captured = capsys.readouterr()
    assert status == 0
    assert "status=verified" in captured.out
    assert written.archive_sha256 in captured.out
    assert str(path) not in captured.out
    assert captured.err == ""


def test_verify_failure_uses_fixed_code_without_path_or_file_content(tmp_path, capsys) -> None:
    path = tmp_path / "private-path-marker.jsonl"
    path.write_text("payload-secret-marker", encoding="utf-8")
    path.chmod(0o600)
    status = audit_retention.main(["verify", "--archive", str(path)])
    captured = capsys.readouterr()
    assert status == 2
    assert "status=failed operation=verify" in captured.err
    assert "private-path-marker" not in captured.err
    assert "payload-secret-marker" not in captured.err


def test_cli_maps_commit_outcome_and_postcommit_failures(monkeypatch, capsys) -> None:
    from app.governance.audit_retention import (
        AuditRetentionCommitOutcomeUnknownError,
        AuditRetentionCommittedVerificationError,
    )

    def commit_unknown(_arguments):
        raise AuditRetentionCommitOutcomeUnknownError(
            "hidden",
            event_count=7,
            archive_sha256="a" * 64,
        )

    monkeypatch.setattr(audit_retention, "_run", commit_unknown)
    assert audit_retention.main(["verify", "--archive", "ignored"]) == 4
    commit_output = capsys.readouterr().err
    assert "status=commit_outcome_unknown" in commit_output
    assert "count=7" in commit_output
    assert f"archive_sha256={'a' * 64}" in commit_output

    def postcommit_failed(_arguments):
        raise AuditRetentionCommittedVerificationError(
            "hidden",
            event_count=8,
            archive_sha256="b" * 64,
        )

    monkeypatch.setattr(audit_retention, "_run", postcommit_failed)
    assert audit_retention.main(["verify", "--archive", "ignored"]) == 3
    postcommit_output = capsys.readouterr().err
    assert "status=committed_but_verification_failed" in postcommit_output
    assert "count=8" in postcommit_output
    assert f"archive_sha256={'b' * 64}" in postcommit_output


def test_verify_fresh_process_ignores_invalid_database_configuration(
    tmp_path,
) -> None:
    archive_path = tmp_path / "offline.jsonl"
    written = write_archive(
        archive_path,
        (),
        cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_alembic_head="20260828_0005",
        has_more_eligible=False,
    )
    backend_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment["LLMBENCHLAB_DATABASE_URL"] = "not-a-sqlalchemy-url"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli.audit_retention",
            "verify",
            "--archive",
            str(archive_path),
            "--expected-sha256",
            written.archive_sha256,
        ],
        cwd=backend_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "status=verified operation=verify count=0" in completed.stdout
    assert "not-a-sqlalchemy-url" not in completed.stdout + completed.stderr
    assert completed.stderr == ""


def test_verify_fresh_process_does_not_create_database_engine_parent(tmp_path) -> None:
    archive_path = tmp_path / "offline-parent.jsonl"
    written = write_archive(
        archive_path,
        (),
        cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_alembic_head="20260828_0005",
        has_more_eligible=False,
    )
    database_parent = tmp_path / "must-not-be-created"
    database_path = database_parent / "offline.db"
    backend_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment["LLMBENCHLAB_DATABASE_URL"] = f"sqlite:///{database_path}"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli.audit_retention",
            "verify",
            "--archive",
            str(archive_path),
            "--expected-sha256",
            written.archive_sha256,
        ],
        cwd=backend_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert not database_parent.exists()


def test_parser_failure_is_fixed_and_never_reflects_unknown_values(capsys) -> None:
    marker = "/private/archive-path-marker.jsonl"

    status = audit_retention.main(["verify", "--archive", "/safe", "--unexpected", marker])
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert captured.err == (
        "status=failed operation=parse error_code=audit_retention_argument_invalid "
        "error_type=AuditRetentionArgumentError\n"
    )
    assert marker not in captured.err


def test_verify_rejects_private_fifo_without_blocking_or_reflecting_path(tmp_path) -> None:
    marker = "secret-fifo-path-marker"
    fifo_path = tmp_path / marker
    os.mkfifo(fifo_path, mode=0o600)
    backend_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli.audit_retention",
            "verify",
            "--archive",
            str(fifo_path),
        ],
        cwd=backend_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 2
    assert "error_code=archive_input_not_regular" in completed.stderr
    assert marker not in completed.stdout + completed.stderr
