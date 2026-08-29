"""Dependency-free fixed outcome classes for the audit-retention CLI."""

from __future__ import annotations


class AuditRetentionError(RuntimeError):
    """A fixed-code retention preflight or pre-commit failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AuditRetentionOutcomeError(RuntimeError):
    """Base class for outcomes that may already include a durable commit."""

    def __init__(
        self,
        code: str,
        *,
        event_count: int | None = None,
        archive_sha256: str | None = None,
    ) -> None:
        self.code = code
        self.event_count = event_count
        self.archive_sha256 = archive_sha256
        super().__init__(code)


class AuditRetentionCommitOutcomeUnknownError(AuditRetentionOutcomeError):
    """The database did not confirm COMMIT after a successful flush."""


class AuditRetentionCommittedVerificationError(AuditRetentionOutcomeError):
    """COMMIT returned, but independent post-commit verification failed."""
