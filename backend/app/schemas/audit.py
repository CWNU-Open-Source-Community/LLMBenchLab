"""Secret-minimized read schemas for retained Run audit events."""

from datetime import datetime
from typing import Any

from app.models import AuditRetentionClass
from app.schemas.base import ORMModel


class AuditEventRead(ORMModel):
    id: str
    event_type: str
    payload: dict[str, Any]
    retention_class: AuditRetentionClass
    occurred_at: datetime
    expires_at: datetime
    correlation_id: str | None
    run_id: str | None
    model_id: str | None
    question_id: str | None
    worker_id: str | None
    reservation_id: str | None
    attempt: int | None
    provider_attempt: int | None
    lease_token: int | None
    duration_ms: float | None


class AuditEventList(ORMModel):
    items: list[AuditEventRead]
    total: int
    offset: int
    limit: int
