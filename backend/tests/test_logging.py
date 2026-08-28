"""Final serialized logging output is JSON, correlated, and secret-safe."""

from __future__ import annotations

import io
import json
import logging
import math

from app.core.logging import (
    SanitizedJsonFormatter,
    configure_logging,
    correlation_scope,
    request_scope,
)

REQUEST_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000002"
QUESTION_ID = "00000000-0000-4000-8000-000000000003"
WORKER_GENERATION_ID = "00000000-0000-4000-8000-000000000004"


def test_json_formatter_serializes_allowlisted_context_and_redacts_exception_text() -> None:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(SanitizedJsonFormatter())
    logger = logging.getLogger("app.runners.evaluation_runner")
    previous_handlers = list(logger.handlers)
    previous_propagate = logger.propagate
    previous_level = logger.level
    previous_disabled = logger.disabled
    secret = "postgresql://user:password@database/private-provider-body"

    try:
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.disabled = False
        with request_scope(REQUEST_ID), correlation_scope(RUN_ID):
            try:
                raise RuntimeError(secret)
            except RuntimeError:
                logger.exception(
                    "Question processing failed",
                    extra={
                        "event": "question_processing_failed",
                        "run_id": RUN_ID,
                        "question_id": QUESTION_ID,
                        "worker_id": f"worker:test-host:42:{WORKER_GENERATION_ID}",
                        "attempt": 2,
                        "lease_token": 3,
                        "untrusted_detail": secret,
                    },
                )
    finally:
        logger.handlers = previous_handlers
        logger.propagate = previous_propagate
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled

    line = output.getvalue().strip()
    payload = json.loads(line)
    assert payload["message"] == "Question processing failed"
    assert payload["event"] == "question_processing_failed"
    assert payload["request_id"] == REQUEST_ID
    assert payload["correlation_id"] == RUN_ID
    assert payload["run_id"] == RUN_ID
    assert payload["question_id"] == QUESTION_ID
    assert payload["worker_id"] == f"worker:{WORKER_GENERATION_ID}"
    assert payload["attempt"] == 2
    assert payload["lease_token"] == 3
    assert payload["exception_type"] == "suppressed"
    assert secret not in line
    assert "untrusted_detail" not in payload


def test_external_logger_message_and_identity_are_not_serialized() -> None:
    secret = "https://user:password@example.invalid/private?api_key=secret"
    record = logging.LogRecord(
        name=f"httpx.{secret}",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=f"HTTP request failed at {secret}",
        args=(),
        exc_info=None,
    )
    record.request_id = secret
    record.correlation_id = secret
    record.request_path = secret
    record.error_code = secret
    record.run_id = secret
    record.levelname = secret
    record.created = math.inf

    rendered = SanitizedJsonFormatter().format(record)
    payload = json.loads(rendered)

    assert payload["logger"] == "external"
    assert payload["message"] == "External component log event"
    assert payload["level"] == "UNKNOWN"
    assert payload["timestamp"] == "1970-01-01T00:00:00+00:00"
    assert set(payload) == {"timestamp", "level", "logger", "message"}
    assert secret not in rendered


def test_unregistered_app_prefix_cannot_spoof_application_logger() -> None:
    secret = "sk-app-prefix-spoof-must-not-be-reflected"
    record = logging.LogRecord(
        name=f"app.{secret}",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=secret,
        args=(),
        exc_info=None,
    )
    record.event = secret

    rendered = SanitizedJsonFormatter().format(record)

    assert json.loads(rendered)["logger"] == "external"
    assert secret not in rendered


def test_application_structured_fields_reject_attacker_selected_strings() -> None:
    secret = "sk-structured-canary-must-not-be-reflected"
    record = logging.LogRecord(
        name="app.workers.service",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Discarding invalid Run queue notification",
        args=(),
        exc_info=None,
    )
    record.request_id = secret
    record.correlation_id = secret
    record.event = secret
    record.run_id = secret
    record.question_id = secret
    record.worker_id = secret
    record.message_id = secret
    record.request_method = secret
    record.request_path = f"/{secret}"
    record.result = secret
    record.error_code = secret
    record.component = secret
    record.attempt = secret
    record.lease_token = secret
    record.status_code = secret
    record.duration_ms = secret

    rendered = SanitizedJsonFormatter().format(record)
    payload = json.loads(rendered)

    assert payload == {
        "timestamp": payload["timestamp"],
        "level": "WARNING",
        "logger": "app.workers.service",
        "message": "Discarding invalid Run queue notification",
        "event": "unsupported",
        "request_method": "unsupported",
        "request_path": "<unsupported>",
        "result": "unsupported",
        "error_code": "unsupported",
        "component": "unsupported",
    }
    assert secret not in rendered


def test_known_structured_contract_normalizes_exception_family_and_stream_id() -> None:
    record = logging.LogRecord(
        name="app.main",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="API request failed",
        args=(),
        exc_info=None,
    )
    record.event = "api_request_failed"
    record.request_id = REQUEST_ID
    record.request_method = "GET"
    record.request_path = "/live"
    record.message_id = "123-4"
    record.status_code = 500
    record.duration_ms = 1.25
    record.error_code = "api_error:RuntimeError"
    record.result = "internal_server_error"

    payload = json.loads(SanitizedJsonFormatter().format(record))

    assert payload["request_id"] == REQUEST_ID
    assert payload["request_method"] == "GET"
    assert payload["request_path"] == "/live"
    assert payload["message_id"] == "123-4"
    assert payload["status_code"] == 500
    assert payload["duration_ms"] == 1.25
    assert payload["error_code"] == "api_error"
    assert payload["result"] == "internal_server_error"


def test_non_finite_structured_value_is_omitted_and_never_emits_invalid_json() -> None:
    record = logging.LogRecord(
        name="app.main",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Finite structured logging boundary",
        args=(),
        exc_info=None,
    )
    record.duration_ms = math.inf

    rendered = SanitizedJsonFormatter().format(record)

    assert "duration_ms" not in json.loads(rendered)
    assert "Infinity" not in rendered


def test_huge_integer_timestamp_and_duration_fail_closed_without_formatter_error() -> None:
    record = logging.LogRecord(
        name="app.main",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="API request completed",
        args=(),
        exc_info=None,
    )
    record.created = 10**10_000
    record.duration_ms = 10**10_000

    payload = json.loads(SanitizedJsonFormatter().format(record))

    assert payload["timestamp"] == "1970-01-01T00:00:00+00:00"
    assert "duration_ms" not in payload


def test_configure_logging_disables_raw_uvicorn_access_handler() -> None:
    root_logger = logging.getLogger()
    previous_root_handlers = list(root_logger.handlers)
    previous_root_level = root_logger.level
    external_logger_names = (
        "httpcore",
        "httpx",
        "redis",
        "sqlalchemy",
        "sqlalchemy.engine",
        "uvicorn",
        "uvicorn.error",
    )
    previous_external = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
        for name in external_logger_names
    }
    access_logger = logging.getLogger("uvicorn.access")
    previous_disabled = access_logger.disabled
    previous_handlers = list(access_logger.handlers)
    previous_propagate = access_logger.propagate
    try:
        access_logger.disabled = False
        access_logger.handlers = [logging.StreamHandler(io.StringIO())]
        access_logger.propagate = True

        configure_logging("INFO")

        assert access_logger.disabled is True
        assert access_logger.handlers == []
        assert access_logger.propagate is False
    finally:
        root_logger.handlers = previous_root_handlers
        root_logger.setLevel(previous_root_level)
        for name, (handlers, propagate) in previous_external.items():
            external_logger = logging.getLogger(name)
            external_logger.handlers = handlers
            external_logger.propagate = propagate
        access_logger.disabled = previous_disabled
        access_logger.handlers = previous_handlers
        access_logger.propagate = previous_propagate
