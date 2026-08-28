"""Trusted maintenance CLI for bounded LLMBenchLab audit retention archives."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from app.governance.audit_archive import (
    AuditArchiveError,
    confirm_archive_digest,
    verify_archive,
)
from app.governance.audit_retention_errors import (
    AuditRetentionCommitOutcomeUnknownError,
    AuditRetentionCommittedVerificationError,
    AuditRetentionError,
)


class AuditRetentionArgumentError(RuntimeError):
    """A parser failure whose untrusted argv text must not be reflected."""


class _FixedErrorArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise AuditRetentionArgumentError("audit_retention_argument_invalid")


def build_parser() -> argparse.ArgumentParser:
    parser = _FixedErrorArgumentParser(
        description="Archive and reconcile expired LLMBenchLab typed audit events",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    archive = commands.add_parser("archive", help="Write one bounded archive without deleting")
    archive.add_argument("--output", required=True)

    verify = commands.add_parser("verify", help="Verify one archive without a database connection")
    verify.add_argument("--archive", required=True)
    verify.add_argument("--expected-sha256")

    for command in ("reconcile", "restore", "delete"):
        mutation = commands.add_parser(command)
        mutation.add_argument("--archive", required=True)
        mutation.add_argument("--confirm-sha256", required=True)
    return parser


def _database_factory():
    # Kept lazy so the verify command is a genuinely offline file operation.
    from app.db.session import SessionLocal

    return SessionLocal


def _run(arguments: argparse.Namespace) -> int:
    operation = arguments.command
    if operation == "verify":
        archive = verify_archive(
            arguments.archive,
            expected_sha256=arguments.expected_sha256,
        )
        print(
            f"status=verified operation=verify count={len(archive.events)} "
            f"archive_sha256={archive.archive_sha256}"
        )
        return 0

    if operation == "archive":
        from app.governance.audit_retention import archive_expired_events

        result = archive_expired_events(_database_factory(), arguments.output)
        print(
            f"status=archived operation=archive count={result.event_count} "
            f"has_more_eligible={str(result.has_more_eligible).lower()} "
            f"archive_sha256={result.archive_sha256}"
        )
        return 0

    archive = verify_archive(arguments.archive)
    confirm_archive_digest(archive, arguments.confirm_sha256)
    factory = _database_factory()
    if operation == "reconcile":
        from app.governance.audit_retention import reconcile_archive

        result = reconcile_archive(factory, archive)
        print(
            f"status={result.status} operation=reconcile count={result.event_count} "
            f"archive_sha256={archive.archive_sha256}"
        )
        return 0
    if operation == "restore":
        from app.governance.audit_retention import restore_archive

        result = restore_archive(factory, archive)
    elif operation == "delete":
        from app.governance.audit_retention import delete_archive_events

        result = delete_archive_events(factory, archive)
    else:  # argparse constrains this branch.
        raise AssertionError("unsupported audit retention command")
    print(
        f"status={result.status} operation={operation} count={result.event_count} "
        f"changed={result.changed_count} archive_sha256={result.archive_sha256}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
    except AuditRetentionArgumentError as exc:
        print(
            "status=failed operation=parse error_code=audit_retention_argument_invalid "
            f"error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        return 2
    try:
        return _run(arguments)
    except AuditRetentionCommitOutcomeUnknownError as exc:
        count = exc.event_count if exc.event_count is not None else "unknown"
        digest = exc.archive_sha256 or "unavailable"
        print(
            f"status=commit_outcome_unknown operation={arguments.command} "
            f"error_code=audit_retention_commit_outcome_unknown "
            f"count={count} archive_sha256={digest} error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        return 4
    except AuditRetentionCommittedVerificationError as exc:
        count = exc.event_count if exc.event_count is not None else "unknown"
        digest = exc.archive_sha256 or "unavailable"
        print(
            f"status=committed_but_verification_failed operation={arguments.command} "
            f"error_code=audit_retention_postcommit_verification_failed "
            f"count={count} archive_sha256={digest} error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        return 3
    except (AuditArchiveError, AuditRetentionError) as exc:
        print(
            f"status=failed operation={arguments.command} error_code={exc.code} "
            f"error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"status=failed operation={arguments.command} "
            f"error_code=audit_retention_internal_error error_type={type(exc).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
