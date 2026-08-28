"""Lazy public exports for Provider governance and typed audit services.

The package is imported before any ``app.governance.*`` submodule.  Keeping
this boundary lazy lets the archive verifier load its pure validation code
without importing the database-backed repository or constructing an engine.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .audit import (
        AuditEventReadFacts,
        AuditIntegrityError,
        append_audit_event,
        validate_audit_event_for_read,
        validate_audit_event_values_for_read,
        validate_audit_identity_for_read,
        validate_audit_payload_for_read,
    )
    from .integrity import record_governance_integrity_event
    from .repository import (
        DatabaseProviderAttemptController,
        GovernanceBacklogFull,
        GovernanceDeferred,
        GovernanceExhausted,
        GovernanceFenceLost,
        GovernanceIntegrityError,
        GovernanceRepository,
        GovernanceSettlementUnknown,
        provider_scope_key,
    )

__all__ = [
    "AuditEventReadFacts",
    "AuditIntegrityError",
    "DatabaseProviderAttemptController",
    "GovernanceBacklogFull",
    "GovernanceDeferred",
    "GovernanceExhausted",
    "GovernanceFenceLost",
    "GovernanceIntegrityError",
    "GovernanceRepository",
    "GovernanceSettlementUnknown",
    "append_audit_event",
    "provider_scope_key",
    "record_governance_integrity_event",
    "validate_audit_event_for_read",
    "validate_audit_event_values_for_read",
    "validate_audit_identity_for_read",
    "validate_audit_payload_for_read",
]

_EXPORT_MODULES = {
    "AuditEventReadFacts": ".audit",
    "AuditIntegrityError": ".audit",
    "append_audit_event": ".audit",
    "validate_audit_event_for_read": ".audit",
    "validate_audit_event_values_for_read": ".audit",
    "validate_audit_identity_for_read": ".audit",
    "validate_audit_payload_for_read": ".audit",
    "record_governance_integrity_event": ".integrity",
    "DatabaseProviderAttemptController": ".repository",
    "GovernanceBacklogFull": ".repository",
    "GovernanceDeferred": ".repository",
    "GovernanceExhausted": ".repository",
    "GovernanceFenceLost": ".repository",
    "GovernanceIntegrityError": ".repository",
    "GovernanceRepository": ".repository",
    "GovernanceSettlementUnknown": ".repository",
    "provider_scope_key": ".repository",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
