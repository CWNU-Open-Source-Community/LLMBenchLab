"""Closed domain enumerations persisted by the MVP."""

from enum import StrEnum


class ProviderType(StrEnum):
    MOCK = "mock"
    OPENAI_COMPATIBLE = "openai_compatible"


class CredentialSource(StrEnum):
    NONE = "none"
    ENVIRONMENT = "environment"
    STORED = "stored"


class QuestionType(StrEnum):
    EXACT_MATCH = "exact_match"
    MULTIPLE_CHOICE = "multiple_choice"
    NUMERIC = "numeric"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})
