"""Final serialized logging output is JSON, correlated, and secret-safe."""

from __future__ import annotations

import io
import json
import logging

from app.core.logging import SanitizedJsonFormatter, correlation_scope, request_scope


def test_json_formatter_serializes_allowlisted_context_and_redacts_exception_text() -> None:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(SanitizedJsonFormatter())
    logger = logging.getLogger("llmbenchlab-test-json")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    secret = "postgresql://user:password@database/private-provider-body"

    with request_scope("request-1"), correlation_scope("run-1"):
        try:
            raise RuntimeError(secret)
        except RuntimeError:
            logger.exception(
                "Sanitized operation failed",
                extra={
                    "event": "operation_failed",
                    "run_id": "run-1",
                    "question_id": "question-1",
                    "worker_id": "worker-1",
                    "attempt": 2,
                    "lease_token": 3,
                    "untrusted_detail": secret,
                },
            )

    line = output.getvalue().strip()
    payload = json.loads(line)
    assert payload["message"] == "Sanitized operation failed"
    assert payload["event"] == "operation_failed"
    assert payload["request_id"] == "request-1"
    assert payload["correlation_id"] == "run-1"
    assert payload["run_id"] == "run-1"
    assert payload["question_id"] == "question-1"
    assert payload["worker_id"] == "worker-1"
    assert payload["attempt"] == 2
    assert payload["lease_token"] == 3
    assert payload["exception_type"] == "RuntimeError"
    assert secret not in line
    assert "untrusted_detail" not in payload
