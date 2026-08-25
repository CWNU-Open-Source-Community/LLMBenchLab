"""Sanitized JSON logging and correlation context for API and Worker processes."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

REQUEST_ID_HEADER = "X-Request-ID"
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_STRUCTURED_FIELDS = (
    "event",
    "run_id",
    "question_id",
    "worker_id",
    "attempt",
    "lease_token",
    "message_id",
    "request_method",
    "request_path",
    "status_code",
    "duration_ms",
    "result",
    "error_code",
    "component",
)


def new_correlation_id() -> str:
    return str(uuid4())


def normalize_correlation_id(value: str | None) -> str | None:
    if value is None or not 1 <= len(value) <= 128:
        return None
    if not all(character.isalnum() or character in "-._:" for character in value):
        return None
    return value


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def get_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def correlation_scope(value: str) -> Iterator[None]:
    token: Token[str | None] = _correlation_id.set(value)
    try:
        yield
    finally:
        _correlation_id.reset(token)


@contextmanager
def request_scope(value: str) -> Iterator[None]:
    token: Token[str | None] = _request_id.set(value)
    try:
        yield
    finally:
        _request_id.reset(token)


def _json_value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class SanitizedJsonFormatter(logging.Formatter):
    """Emit allowlisted context without exception text, request bodies, or credentials."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = get_request_id() or getattr(record, "request_id", None)
        if request_id is not None:
            payload["request_id"] = _json_value(request_id)
        correlation_id = get_correlation_id() or getattr(record, "correlation_id", None)
        if correlation_id is not None:
            payload["correlation_id"] = _json_value(correlation_id)
        for field in _STRUCTURED_FIELDS:
            if hasattr(record, field):
                payload[field] = _json_value(getattr(record, field))
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SanitizedJsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
