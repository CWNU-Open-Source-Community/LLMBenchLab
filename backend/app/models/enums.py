"""Closed domain enumerations persisted by the MVP."""

from enum import StrEnum


class ProviderType(StrEnum):
    MOCK = "mock"
    OPENAI_COMPATIBLE = "openai_compatible"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


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


class GovernanceRunStatus(StrEnum):
    LEGACY_UNMANAGED = "legacy_unmanaged"
    MANAGED = "managed"
    DELAYED = "delayed"
    EXHAUSTED = "exhausted"


class GovernanceScopeType(StrEnum):
    GLOBAL = "global"
    PROVIDER = "provider"
    MODEL = "model"
    RUN = "run"


class ProviderCallReservationState(StrEnum):
    RESERVED = "reserved"
    SEND_STARTED = "send_started"
    SETTLED_ACTUAL = "settled_actual"
    SETTLED_CONSERVATIVE = "settled_conservative"
    RELEASED_PRE_SEND = "released_pre_send"


class AuditRetentionClass(StrEnum):
    OPERATIONAL = "operational"
    SECURITY = "security"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})
