"""Sanitized JSON logging and correlation context for API and Worker processes."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any
from uuid import uuid4

from app.core.logging_contract import (
    OMIT as _OMIT,
)
from app.core.logging_contract import (
    is_application_logger,
    normalize_application_message,
    normalize_correlation_id,
    normalize_log_level,
    normalize_redis_stream_id,
    normalize_request_method,
    normalize_request_path,
    normalize_structured_field,
    normalize_timestamp,
    normalize_uuid_identifier,
)

__all__ = (
    "REQUEST_ID_HEADER",
    "SanitizedJsonFormatter",
    "configure_logging",
    "correlation_scope",
    "get_correlation_id",
    "get_request_id",
    "new_correlation_id",
    "normalize_correlation_id",
    "normalize_redis_stream_id",
    "normalize_request_method",
    "normalize_request_path",
    "normalize_uuid_identifier",
    "request_scope",
)

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
_EXTERNAL_LOGGER_NAME = "external"
_EXTERNAL_MESSAGE = "External component log event"


def new_correlation_id() -> str:
    return str(uuid4())


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


class SanitizedJsonFormatter(logging.Formatter):
    """Emit only closed-domain structured context and fixed application messages."""

    def format(self, record: logging.LogRecord) -> str:
        application_record = is_application_logger(record.name)
        payload: dict[str, Any] = {
            "timestamp": normalize_timestamp(record.created),
            "level": normalize_log_level(record.levelname),
            "logger": record.name if application_record else _EXTERNAL_LOGGER_NAME,
            # Registered application sources are statically restricted to this
            # closed literal-message set. External dynamic identity, messages,
            # formatting arguments, exception text, and extras are suppressed.
            "message": (
                normalize_application_message(record) if application_record else _EXTERNAL_MESSAGE
            ),
        }
        request_id = get_request_id()
        if request_id is None and application_record:
            request_id = getattr(record, "request_id", None)
        normalized_request_id = normalize_uuid_identifier(request_id)
        if normalized_request_id is not None:
            payload["request_id"] = normalized_request_id

        correlation_id = get_correlation_id()
        if correlation_id is None and application_record:
            correlation_id = getattr(record, "correlation_id", None)
        normalized_correlation_id = normalize_correlation_id(correlation_id)
        if normalized_correlation_id is not None:
            payload["correlation_id"] = normalized_correlation_id

        if application_record:
            for field in _STRUCTURED_FIELDS:
                if hasattr(record, field):
                    normalized = normalize_structured_field(field, getattr(record, field))
                    if normalized is not _OMIT:
                        payload[field] = normalized
        if application_record and isinstance(record.exc_info, tuple) and record.exc_info:
            payload["exception_type"] = "suppressed"
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SanitizedJsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # Uvicorn installs named handlers before importing the application. Its
    # access message can contain a raw query string, while dependency loggers
    # may contain URLs or driver errors. Route all of them through the sanitized
    # root formatter and disable the redundant raw access log; the application
    # middleware already emits a route-template request completion event.
    for logger_name in (
        "httpcore",
        "httpx",
        "redis",
        "sqlalchemy",
        "sqlalchemy.engine",
        "uvicorn",
        "uvicorn.error",
    ):
        external_logger = logging.getLogger(logger_name)
        external_logger.handlers.clear()
        external_logger.propagate = True
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True
