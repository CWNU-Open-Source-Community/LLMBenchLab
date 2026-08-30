"""Explicit OpenAI Responses and Anthropic Messages protocol adapters."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from contextlib import aclosing
from typing import Any, Protocol, TypeVar

import httpx

from .base import AdapterError, GenerationConfig, Message, ModelGenerationResult
from .openai_compatible import (
    MAX_CHAT_STREAM_EVENT_BYTES,
    MAX_CHAT_STREAM_WIRE_BYTES,
    MAX_CHAT_SUCCESS_RESPONSE_BYTES,
    OpenAICompatibleAdapter,
    _redact_json_secret,
    _ResponseBodyTooLarge,
    _safe_status_code,
    sanitize_error_message,
)


class _TypedSSEAccumulator(Protocol):
    done: bool
    attempts: int
    api_key: str

    def consume_event(self, raw_event: bytes, event_name: str | None) -> None: ...


_AccumulatorT = TypeVar("_AccumulatorT", bound=_TypedSSEAccumulator)
_RESPONSES_RATE_LIMIT_ERROR_CODES = frozenset(
    {"rate_limit", "rate_limit_error", "rate_limit_exceeded"}
)
_RESPONSES_SERVER_ERROR_CODES = frozenset({"server_error"})
_MESSAGES_RATE_LIMIT_ERROR_CODES = frozenset({"rate_limit_error"})
_MESSAGES_SERVER_ERROR_CODES = frozenset({"api_error", "overloaded_error", "timeout_error"})


def _invalid_stream(message: str, *, status_code: int, attempts: int, api_key: str) -> AdapterError:
    return AdapterError(
        "invalid_provider_stream",
        message,
        status_code=_safe_status_code(status_code, api_key),
        attempts=attempts,
    )


def _decode_typed_event(
    raw_event: bytes,
    event_name: str | None,
    *,
    status_code: int,
    attempts: int,
    api_key: str,
) -> tuple[str, Mapping[str, Any]]:
    invalid_utf8 = False
    try:
        event_text = raw_event.decode("utf-8")
    except UnicodeError:
        invalid_utf8 = True
        event_text = ""
    if invalid_utf8:
        raise _invalid_stream(
            "Upstream SSE data was not valid UTF-8.",
            status_code=status_code,
            attempts=attempts,
            api_key=api_key,
        )
    invalid_json = False
    try:
        body = json.loads(event_text)
    except ValueError:
        invalid_json = True
        body = None
    if invalid_json:
        raise _invalid_stream(
            "Upstream SSE data was not valid JSON.",
            status_code=status_code,
            attempts=attempts,
            api_key=api_key,
        )
    if not isinstance(body, Mapping):
        raise _invalid_stream(
            "Upstream SSE data was not a JSON object.",
            status_code=status_code,
            attempts=attempts,
            api_key=api_key,
        )

    body_type = body.get("type")
    if body_type is not None and not isinstance(body_type, str):
        raise _invalid_stream(
            "Upstream SSE event type was not text.",
            status_code=status_code,
            attempts=attempts,
            api_key=api_key,
        )
    if event_name and body_type and event_name != body_type:
        raise _invalid_stream(
            "Upstream SSE event name conflicted with its JSON type.",
            status_code=status_code,
            attempts=attempts,
            api_key=api_key,
        )
    event_type = body_type or event_name
    if not event_type:
        raise _invalid_stream(
            "Upstream SSE event did not declare a type.",
            status_code=status_code,
            attempts=attempts,
            api_key=api_key,
        )
    return event_type, body


async def _consume_typed_sse(
    response: httpx.Response,
    accumulator: _AccumulatorT,
    *,
    terminal_event: str,
) -> _AccumulatorT:
    """Consume bounded SSE framing while delegating typed event semantics."""

    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > MAX_CHAT_STREAM_WIRE_BYTES:
            raise _ResponseBodyTooLarge(
                limit=MAX_CHAT_STREAM_WIRE_BYTES,
                status_code=response.status_code,
            )

    line_buffer = bytearray()
    data_lines: list[bytes] = []
    event_name: str | None = None
    event_bytes = 0
    wire_bytes = 0
    first_line = True

    def dispatch_event() -> None:
        nonlocal event_name
        if data_lines:
            accumulator.consume_event(b"\n".join(data_lines), event_name)
            data_lines.clear()
        event_name = None

    def process_line(line: bytes) -> None:
        nonlocal event_bytes, event_name, first_line
        raw_line_size = len(line)
        if first_line:
            first_line = False
            if line.startswith(b"\xef\xbb\xbf"):
                line = line[3:]
        if not line:
            dispatch_event()
            event_bytes = 0
            return

        event_bytes += raw_line_size + 1
        if event_bytes > MAX_CHAT_STREAM_EVENT_BYTES:
            raise _ResponseBodyTooLarge(
                limit=MAX_CHAT_STREAM_EVENT_BYTES,
                status_code=response.status_code,
            )
        if line.startswith(b":"):
            return
        field, separator, value = line.partition(b":")
        if separator and value.startswith(b" "):
            value = value[1:]
        if field == b"data":
            data_lines.append(value if separator else b"")
            return
        if field != b"event":
            return
        invalid_event_name = False
        try:
            decoded_name = (value if separator else b"").decode("utf-8")
        except UnicodeError:
            invalid_event_name = True
            decoded_name = ""
        if invalid_event_name:
            raise _invalid_stream(
                "Upstream SSE event name was not valid UTF-8.",
                status_code=response.status_code,
                attempts=accumulator.attempts,
                api_key=accumulator.api_key,
            )
        event_name = decoded_name or None

    def process_chunk(chunk: bytes) -> bool:
        nonlocal wire_bytes
        wire_bytes += len(chunk)
        if wire_bytes > MAX_CHAT_STREAM_WIRE_BYTES:
            raise _ResponseBodyTooLarge(
                limit=MAX_CHAT_STREAM_WIRE_BYTES,
                status_code=response.status_code,
            )
        line_buffer.extend(chunk)
        while True:
            line = OpenAICompatibleAdapter._pop_sse_line(line_buffer)
            if line is None:
                break
            process_line(line)
            if accumulator.done:
                return True
        if event_bytes + len(line_buffer) > MAX_CHAT_STREAM_EVENT_BYTES:
            raise _ResponseBodyTooLarge(
                limit=MAX_CHAT_STREAM_EVENT_BYTES,
                status_code=response.status_code,
            )
        return False

    if response.is_stream_consumed:
        process_chunk(response.content)
    else:
        async with aclosing(response.aiter_raw()) as chunks:
            async for chunk in chunks:
                if process_chunk(chunk):
                    break
    if accumulator.done:
        return accumulator

    while True:
        line = OpenAICompatibleAdapter._pop_sse_line(line_buffer, at_eof=True)
        if line is None:
            break
        process_line(line)
        if accumulator.done:
            return accumulator
    dispatch_event()
    if not accumulator.done:
        raise AdapterError(
            "incomplete_provider_stream",
            f"Upstream SSE response ended before the {terminal_event} event.",
            status_code=_safe_status_code(
                response.status_code,
                accumulator.api_key,
            ),
            attempts=accumulator.attempts,
        )
    return accumulator


def _parse_json_object(
    response: httpx.Response,
    response_body: bytes,
    *,
    attempts: int,
    api_key: str,
) -> Mapping[str, Any]:
    invalid_json = False
    try:
        body = json.loads(response_body)
    except (ValueError, UnicodeError):
        invalid_json = True
        body = None
    if invalid_json:
        raise AdapterError(
            "invalid_provider_response",
            "Upstream returned a non-JSON success response.",
            status_code=_safe_status_code(response.status_code, api_key),
            attempts=attempts,
        )
    if not isinstance(body, Mapping):
        raise AdapterError(
            "invalid_provider_response",
            "Upstream success response was not a JSON object.",
            status_code=_safe_status_code(response.status_code, api_key),
            attempts=attempts,
        )
    return body


def _append_text(
    content: io.StringIO,
    text: object,
    *,
    content_bytes: int,
    status_code: int,
    attempts: int,
    api_key: str,
    source: str,
) -> int:
    if not isinstance(text, str):
        raise _invalid_stream(
            f"Upstream SSE {source} was not text.",
            status_code=status_code,
            attempts=attempts,
            api_key=api_key,
        )
    invalid_text = False
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeError:
        invalid_text = True
        encoded_size = 0
    if invalid_text:
        raise _invalid_stream(
            f"Upstream SSE {source} was not valid Unicode text.",
            status_code=status_code,
            attempts=attempts,
            api_key=api_key,
        )
    if content_bytes + encoded_size > MAX_CHAT_SUCCESS_RESPONSE_BYTES:
        raise _ResponseBodyTooLarge(
            limit=MAX_CHAT_SUCCESS_RESPONSE_BYTES,
            status_code=status_code,
        )
    content.write(text)
    return content_bytes + encoded_size


def _collect_response_output_text(output: object) -> str:
    if not isinstance(output, list):
        raise ValueError("output was not an array")
    parts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            raise ValueError("output item was not an object")
        item_type = item.get("type")
        if item_type == "output_text":
            text = item.get("text")
            if not isinstance(text, str):
                raise ValueError("output_text item did not contain text")
            parts.append(text)
            continue
        if item_type != "message":
            continue
        blocks = item.get("content")
        if not isinstance(blocks, list):
            raise ValueError("message content was not an array")
        for block in blocks:
            if not isinstance(block, Mapping):
                raise ValueError("message content item was not an object")
            if block.get("type") != "output_text":
                continue
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("output_text block did not contain text")
            parts.append(text)
    return "".join(parts)


def _provider_failure_detail(body: Mapping[str, Any], fallback: str) -> object:
    error = body.get("error")
    if isinstance(error, Mapping):
        return error.get("message") or error.get("type") or fallback
    if error is not None:
        return error
    details = body.get("incomplete_details")
    if isinstance(details, Mapping):
        return details.get("reason") or fallback
    return fallback


def _provider_error_codes(body: Mapping[str, Any]) -> frozenset[str]:
    error = body.get("error")
    candidates: tuple[object, ...]
    if isinstance(error, Mapping):
        candidates = (error.get("type"), error.get("code"), body.get("code"))
    else:
        candidates = (body.get("code"),)
    return frozenset(value for value in candidates if isinstance(value, str))


def _responses_stream_error_type(body: Mapping[str, Any]) -> tuple[str, bool]:
    codes = _provider_error_codes(body)
    if codes & _RESPONSES_RATE_LIMIT_ERROR_CODES:
        return "rate_limited", True
    if codes & _RESPONSES_SERVER_ERROR_CODES:
        return "provider_5xx", True
    return "provider_stream_error", False


def _messages_stream_error_type(body: Mapping[str, Any]) -> tuple[str, bool]:
    codes = _provider_error_codes(body)
    if codes & _MESSAGES_RATE_LIMIT_ERROR_CODES:
        return "rate_limited", True
    if codes & _MESSAGES_SERVER_ERROR_CODES:
        return "provider_5xx", True
    return "provider_stream_error", False


def _build_result(
    response: httpx.Response,
    *,
    content: object,
    usage_obj: object,
    provider_request_id: object,
    returned_model: object,
    finish_reason: object,
    latency_ms: float,
    attempts: int,
    api_key: str,
    response_mode: str,
    adapter_name: str,
) -> ModelGenerationResult:
    if not isinstance(content, str):
        raise AdapterError(
            "invalid_provider_response",
            "Upstream response content was not text.",
            status_code=_safe_status_code(response.status_code, api_key),
            attempts=attempts,
        )
    if not content.strip():
        if finish_reason in {"length", "max_tokens", "max_output_tokens"}:
            raise AdapterError(
                "output_truncated",
                "Upstream exhausted the output token budget before returning text. "
                "Increase max_tokens.",
                status_code=_safe_status_code(response.status_code, api_key),
                attempts=attempts,
            )
        raise AdapterError(
            "empty_response",
            "Upstream returned an empty model response.",
            status_code=_safe_status_code(response.status_code, api_key),
            attempts=attempts,
        )
    safe_content = content.replace(api_key, "[REDACTED]")
    raw_usage = (
        _redact_json_secret(dict(usage_obj), api_key) if isinstance(usage_obj, Mapping) else None
    )
    input_tokens = OpenAICompatibleAdapter._usage_int(raw_usage, "input_tokens")
    output_tokens = OpenAICompatibleAdapter._usage_int(raw_usage, "output_tokens")
    if provider_request_id is None:
        provider_request_id = response.headers.get("x-request-id") or response.headers.get(
            "request-id"
        )
    return ModelGenerationResult(
        text=safe_content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=max(0.0, latency_ms),
        provider_request_id=(
            None
            if provider_request_id is None
            else sanitize_error_message(provider_request_id, api_key)
        ),
        raw_usage=raw_usage,
        metadata={
            "adapter": adapter_name,
            "attempts": attempts,
            "response_mode": response_mode,
            "finish_reason": (
                sanitize_error_message(finish_reason, api_key)
                if isinstance(finish_reason, str)
                else None
            ),
            "returned_model": (
                sanitize_error_message(returned_model, api_key)
                if isinstance(returned_model, str)
                else None
            ),
        },
    )


class _ResponsesSSEAccumulator:
    def __init__(self, *, status_code: int, attempts: int, api_key: str) -> None:
        self.status_code = status_code
        self.attempts = attempts
        self.api_key = api_key
        self._content = io.StringIO()
        self.content_bytes = 0
        self.usage: Mapping[str, Any] | None = None
        self.provider_request_id: str | None = None
        self.returned_model: str | None = None
        self.system_fingerprint: str | None = None
        self.finish_reason: str | None = None
        self.done = False

    @property
    def content(self) -> str:
        return self._content.getvalue()

    def consume_event(self, raw_event: bytes, event_name: str | None) -> None:
        event_type, body = _decode_typed_event(
            raw_event,
            event_name,
            status_code=self.status_code,
            attempts=self.attempts,
            api_key=self.api_key,
        )
        if event_type == "error":
            detail = _provider_failure_detail(body, "Provider reported a Responses stream error.")
            error_type, retryable = _responses_stream_error_type(body)
            raise AdapterError(
                error_type,
                sanitize_error_message(f"Upstream SSE error: {detail}", self.api_key),
                retryable=retryable,
                status_code=_safe_status_code(self.status_code, self.api_key),
                attempts=self.attempts,
            )
        if event_type in {"response.failed", "response.incomplete"}:
            response_obj = body.get("response")
            detail_body = response_obj if isinstance(response_obj, Mapping) else body
            detail = _provider_failure_detail(detail_body, event_type)
            if event_type == "response.failed":
                error_type, retryable = _responses_stream_error_type(detail_body)
            elif detail == "max_output_tokens":
                error_type = "output_truncated"
                retryable = False
            else:
                error_type = "incomplete_provider_stream"
                retryable = False
            raise AdapterError(
                error_type,
                sanitize_error_message(
                    f"Upstream Responses stream did not complete: {detail}",
                    self.api_key,
                ),
                retryable=retryable,
                status_code=_safe_status_code(self.status_code, self.api_key),
                attempts=self.attempts,
            )
        if event_type == "response.output_text.delta":
            self.content_bytes = _append_text(
                self._content,
                body.get("delta"),
                content_bytes=self.content_bytes,
                status_code=self.status_code,
                attempts=self.attempts,
                api_key=self.api_key,
                source="output_text delta",
            )
            return
        if event_type not in {
            "response.created",
            "response.in_progress",
            "response.completed",
        }:
            return
        response_obj = body.get("response")
        if not isinstance(response_obj, Mapping):
            raise _invalid_stream(
                f"Upstream SSE {event_type} response was not an object.",
                status_code=self.status_code,
                attempts=self.attempts,
                api_key=self.api_key,
            )
        self._capture_response(response_obj)
        if event_type != "response.completed":
            return
        status = response_obj.get("status")
        if status is not None and status != "completed":
            detail = _provider_failure_detail(response_obj, str(status))
            raise AdapterError(
                "incomplete_provider_stream",
                sanitize_error_message(
                    f"Upstream Responses stream did not complete: {detail}", self.api_key
                ),
                status_code=_safe_status_code(self.status_code, self.api_key),
                attempts=self.attempts,
            )
        if not self.content:
            invalid_completed_output = False
            try:
                completed_text = _collect_response_output_text(response_obj.get("output"))
            except ValueError:
                invalid_completed_output = True
                completed_text = ""
            if invalid_completed_output:
                raise _invalid_stream(
                    "Upstream SSE response.completed output was invalid.",
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                )
            self.content_bytes = _append_text(
                self._content,
                completed_text,
                content_bytes=self.content_bytes,
                status_code=self.status_code,
                attempts=self.attempts,
                api_key=self.api_key,
                source="completed output",
            )
        self.finish_reason = "completed"
        self.done = True

    def _capture_response(self, response_obj: Mapping[str, Any]) -> None:
        self.provider_request_id = self._stable_string(
            response_obj.get("id"), self.provider_request_id, "response id"
        )
        self.returned_model = self._stable_string(
            response_obj.get("model"), self.returned_model, "response model"
        )
        usage = response_obj.get("usage")
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise _invalid_stream(
                    "Upstream SSE Responses usage was not an object.",
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                )
            self.usage = dict(usage)

    def _stable_string(
        self,
        value: object,
        existing: str | None,
        label: str,
    ) -> str | None:
        if value is None:
            return existing
        if not isinstance(value, str):
            raise _invalid_stream(
                f"Upstream SSE {label} was not text.",
                status_code=self.status_code,
                attempts=self.attempts,
                api_key=self.api_key,
            )
        if existing is not None and existing != value:
            raise _invalid_stream(
                f"Upstream SSE returned conflicting {label} values.",
                status_code=self.status_code,
                attempts=self.attempts,
                api_key=self.api_key,
            )
        return value


class OpenAIResponsesAdapter(OpenAICompatibleAdapter):
    """Call an explicit OpenAI Responses ``/responses`` endpoint."""

    _endpoint_suffix = "/responses"
    _transport_error_label = "OpenAI Responses"

    @property
    def responses_url(self) -> str:
        return self.endpoint_url

    def _build_payload(
        self,
        messages: Sequence[Message],
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        if generation_config.get("seed") is not None:
            raise AdapterError(
                "invalid_request",
                "OpenAI Responses does not support the configured seed parameter.",
            )
        payload = super()._build_payload(messages, generation_config)
        payload["input"] = payload.pop("messages")
        payload.pop("stream_options", None)
        payload.pop("seed", None)
        if "max_tokens" in payload:
            payload["max_output_tokens"] = payload.pop("max_tokens")
        return payload

    @staticmethod
    async def _consume_sse_response(
        response: httpx.Response,
        *,
        attempts: int,
        api_key: str,
    ) -> _ResponsesSSEAccumulator:
        accumulator = _ResponsesSSEAccumulator(
            status_code=response.status_code,
            attempts=attempts,
            api_key=api_key,
        )
        return await _consume_typed_sse(
            response,
            accumulator,
            terminal_event="response.completed",
        )

    @staticmethod
    def _parse_success_response(
        response: httpx.Response,
        response_body: bytes,
        *,
        latency_ms: float,
        attempts: int,
        api_key: str,
    ) -> ModelGenerationResult:
        body = _parse_json_object(
            response,
            response_body,
            attempts=attempts,
            api_key=api_key,
        )
        status = body.get("status")
        if status is not None and not isinstance(status, str):
            raise AdapterError(
                "invalid_provider_response",
                "Upstream Responses status was not text.",
                status_code=_safe_status_code(response.status_code, api_key),
                attempts=attempts,
            )
        if status != "completed" and status is not None:
            detail = _provider_failure_detail(body, status)
            retryable = False
            if status == "failed":
                error_type, retryable = _responses_stream_error_type(body)
                if error_type == "provider_stream_error":
                    error_type = "invalid_provider_response"
            elif detail == "max_output_tokens":
                error_type = "output_truncated"
            else:
                error_type = "invalid_provider_response"
            raise AdapterError(
                error_type,
                sanitize_error_message(
                    f"Upstream Responses request did not complete: {detail}", api_key
                ),
                retryable=retryable,
                status_code=_safe_status_code(response.status_code, api_key),
                attempts=attempts,
            )
        invalid_output = False
        try:
            content = _collect_response_output_text(body.get("output"))
        except ValueError:
            invalid_output = True
            content = ""
        if invalid_output:
            raise AdapterError(
                "invalid_provider_response",
                "Upstream Responses output did not contain valid output text items.",
                status_code=_safe_status_code(response.status_code, api_key),
                attempts=attempts,
            )
        return _build_result(
            response,
            content=content,
            usage_obj=body.get("usage"),
            provider_request_id=body.get("id"),
            returned_model=body.get("model"),
            finish_reason=status,
            latency_ms=latency_ms,
            attempts=attempts,
            api_key=api_key,
            response_mode="json",
            adapter_name="openai_responses",
        )

    @staticmethod
    def _build_generation_result(
        response: httpx.Response,
        *,
        content: object,
        finish_reason: object,
        usage_obj: object,
        provider_request_id: object,
        returned_model: object,
        system_fingerprint: object,
        latency_ms: float,
        attempts: int,
        api_key: str,
        response_mode: str,
    ) -> ModelGenerationResult:
        del system_fingerprint
        return _build_result(
            response,
            content=content,
            usage_obj=usage_obj,
            provider_request_id=provider_request_id,
            returned_model=returned_model,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            attempts=attempts,
            api_key=api_key,
            response_mode=response_mode,
            adapter_name="openai_responses",
        )


class _MessagesSSEAccumulator:
    def __init__(self, *, status_code: int, attempts: int, api_key: str) -> None:
        self.status_code = status_code
        self.attempts = attempts
        self.api_key = api_key
        self._content = io.StringIO()
        self.content_bytes = 0
        self.usage: dict[str, Any] = {}
        self.provider_request_id: str | None = None
        self.returned_model: str | None = None
        self.system_fingerprint: str | None = None
        self.finish_reason: str | None = None
        self.saw_message_start = False
        self.done = False

    @property
    def content(self) -> str:
        return self._content.getvalue()

    def consume_event(self, raw_event: bytes, event_name: str | None) -> None:
        event_type, body = _decode_typed_event(
            raw_event,
            event_name,
            status_code=self.status_code,
            attempts=self.attempts,
            api_key=self.api_key,
        )
        if event_type == "error":
            detail = _provider_failure_detail(body, "Provider reported a Messages stream error.")
            error_type, retryable = _messages_stream_error_type(body)
            raise AdapterError(
                error_type,
                sanitize_error_message(f"Upstream SSE error: {detail}", self.api_key),
                retryable=retryable,
                status_code=_safe_status_code(self.status_code, self.api_key),
                attempts=self.attempts,
            )
        if event_type == "message_start":
            if self.saw_message_start:
                raise _invalid_stream(
                    "Upstream SSE returned more than one message_start event.",
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                )
            message = body.get("message")
            if not isinstance(message, Mapping):
                raise _invalid_stream(
                    "Upstream SSE message_start message was not an object.",
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                )
            self.saw_message_start = True
            self.provider_request_id = self._optional_string(message.get("id"), "message id")
            self.returned_model = self._optional_string(message.get("model"), "message model")
            self.finish_reason = self._optional_string(message.get("stop_reason"), "stop reason")
            self._merge_usage(message.get("usage"))
            self._consume_initial_content(message.get("content"))
            return
        if event_type == "content_block_delta":
            if not self.saw_message_start:
                raise _invalid_stream(
                    "Upstream SSE content_block_delta preceded message_start.",
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                )
            delta = body.get("delta")
            if not isinstance(delta, Mapping):
                raise _invalid_stream(
                    "Upstream SSE content_block_delta delta was not an object.",
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                )
            if delta.get("type") == "text_delta":
                self.content_bytes = _append_text(
                    self._content,
                    delta.get("text"),
                    content_bytes=self.content_bytes,
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                    source="text delta",
                )
            return
        if event_type == "message_delta":
            if not self.saw_message_start:
                raise _invalid_stream(
                    "Upstream SSE message_delta preceded message_start.",
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                )
            delta = body.get("delta")
            if not isinstance(delta, Mapping):
                raise _invalid_stream(
                    "Upstream SSE message_delta delta was not an object.",
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                )
            stop_reason = self._optional_string(delta.get("stop_reason"), "stop reason")
            if (
                stop_reason is not None
                and self.finish_reason is not None
                and stop_reason != self.finish_reason
            ):
                raise _invalid_stream(
                    "Upstream SSE returned conflicting stop reason values.",
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                )
            if stop_reason is not None:
                self.finish_reason = stop_reason
            self._merge_usage(body.get("usage"))
            return
        if event_type == "message_stop":
            if not self.saw_message_start:
                raise _invalid_stream(
                    "Upstream SSE message_stop preceded message_start.",
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                )
            self.done = True

    def _optional_string(self, value: object, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise _invalid_stream(
                f"Upstream SSE {label} was not text.",
                status_code=self.status_code,
                attempts=self.attempts,
                api_key=self.api_key,
            )
        return value

    def _merge_usage(self, usage: object) -> None:
        if usage is None:
            return
        if not isinstance(usage, Mapping):
            raise _invalid_stream(
                "Upstream SSE Messages usage was not an object.",
                status_code=self.status_code,
                attempts=self.attempts,
                api_key=self.api_key,
            )
        # Anthropic stream usage is cumulative/delta-by-field. Overwrite each
        # supplied field; summing repeated output_tokens would double count.
        self.usage.update(usage)

    def _consume_initial_content(self, content: object) -> None:
        if content is None:
            return
        if not isinstance(content, list):
            raise _invalid_stream(
                "Upstream SSE initial message content was not an array.",
                status_code=self.status_code,
                attempts=self.attempts,
                api_key=self.api_key,
            )
        for block in content:
            if not isinstance(block, Mapping):
                raise _invalid_stream(
                    "Upstream SSE initial content item was not an object.",
                    status_code=self.status_code,
                    attempts=self.attempts,
                    api_key=self.api_key,
                )
            if block.get("type") != "text":
                continue
            self.content_bytes = _append_text(
                self._content,
                block.get("text"),
                content_bytes=self.content_bytes,
                status_code=self.status_code,
                attempts=self.attempts,
                api_key=self.api_key,
                source="initial text",
            )


class AnthropicMessagesAdapter(OpenAICompatibleAdapter):
    """Call an explicit Anthropic Messages ``/messages`` endpoint."""

    _endpoint_suffix = "/messages"
    _transport_error_label = "Anthropic Messages"

    @property
    def messages_url(self) -> str:
        return self.endpoint_url

    @staticmethod
    def _status_error_type(status_code: int) -> tuple[str, bool]:
        if status_code == 529:
            return "provider_5xx", True
        return OpenAICompatibleAdapter._status_error_type(status_code)

    def _build_headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Accept": "text/event-stream",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        messages: Sequence[Message],
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        if generation_config.get("seed") is not None:
            raise AdapterError(
                "invalid_request",
                "Anthropic Messages does not support the configured seed parameter.",
            )
        temperature = generation_config.get("temperature")
        if temperature is not None and (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or temperature != temperature
            or temperature in {float("inf"), float("-inf")}
            or not 0 <= temperature <= 1
        ):
            raise AdapterError(
                "invalid_request",
                "Anthropic Messages requires temperature to be between 0 and 1.",
            )
        top_p = generation_config.get("top_p")
        if top_p is not None and (
            isinstance(top_p, bool)
            or not isinstance(top_p, (int, float))
            or top_p != top_p
            or top_p in {float("inf"), float("-inf")}
            or not 0 < top_p <= 1
        ):
            raise AdapterError(
                "invalid_request",
                "Anthropic Messages requires top_p to be greater than 0 and at most 1.",
            )
        max_tokens = generation_config.get("max_tokens")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise AdapterError(
                "invalid_request",
                "Anthropic Messages requires max_tokens to be a positive integer.",
            )

        chat_payload = super()._build_payload(messages, generation_config)
        prepared_messages = chat_payload["messages"]
        system_parts: list[str] = []
        body_messages: list[dict[str, Any]] = []
        for index, message in enumerate(prepared_messages):
            role = message.get("role")
            if role == "system":
                if body_messages:
                    raise AdapterError(
                        "invalid_request",
                        "Anthropic Messages accepts system instructions only before messages.",
                    )
                content = message.get("content")
                if not isinstance(content, str):
                    raise AdapterError(
                        "invalid_request",
                        f"System message at index {index} must contain text.",
                    )
                if content.strip():
                    system_parts.append(content)
                continue
            if role not in {"user", "assistant"}:
                raise AdapterError(
                    "invalid_request",
                    f"Anthropic message at index {index} has unsupported role {role!r}.",
                )
            body_messages.append({"role": role, "content": message.get("content")})
        if not body_messages:
            raise AdapterError("invalid_request", "At least one non-system message is required.")

        payload: dict[str, Any] = {
            "model": self.remote_model_name,
            "messages": body_messages,
            "stream": True,
            "max_tokens": max_tokens,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        for key in ("temperature", "top_p"):
            value = generation_config.get(key)
            if value is not None:
                payload[key] = value
        return payload

    @staticmethod
    async def _consume_sse_response(
        response: httpx.Response,
        *,
        attempts: int,
        api_key: str,
    ) -> _MessagesSSEAccumulator:
        accumulator = _MessagesSSEAccumulator(
            status_code=response.status_code,
            attempts=attempts,
            api_key=api_key,
        )
        return await _consume_typed_sse(
            response,
            accumulator,
            terminal_event="message_stop",
        )

    @staticmethod
    def _parse_success_response(
        response: httpx.Response,
        response_body: bytes,
        *,
        latency_ms: float,
        attempts: int,
        api_key: str,
    ) -> ModelGenerationResult:
        body = _parse_json_object(
            response,
            response_body,
            attempts=attempts,
            api_key=api_key,
        )
        content_obj = body.get("content")
        if not isinstance(content_obj, list):
            raise AdapterError(
                "invalid_provider_response",
                "Upstream Messages response content was not an array.",
                status_code=_safe_status_code(response.status_code, api_key),
                attempts=attempts,
            )
        parts: list[str] = []
        for block in content_obj:
            if not isinstance(block, Mapping):
                raise AdapterError(
                    "invalid_provider_response",
                    "Upstream Messages content item was not an object.",
                    status_code=_safe_status_code(response.status_code, api_key),
                    attempts=attempts,
                )
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str):
                raise AdapterError(
                    "invalid_provider_response",
                    "Upstream Messages text block did not contain text.",
                    status_code=_safe_status_code(response.status_code, api_key),
                    attempts=attempts,
                )
            parts.append(text)
        return _build_result(
            response,
            content="".join(parts),
            usage_obj=body.get("usage"),
            provider_request_id=body.get("id"),
            returned_model=body.get("model"),
            finish_reason=body.get("stop_reason"),
            latency_ms=latency_ms,
            attempts=attempts,
            api_key=api_key,
            response_mode="json",
            adapter_name="anthropic_messages",
        )

    @staticmethod
    def _build_generation_result(
        response: httpx.Response,
        *,
        content: object,
        finish_reason: object,
        usage_obj: object,
        provider_request_id: object,
        returned_model: object,
        system_fingerprint: object,
        latency_ms: float,
        attempts: int,
        api_key: str,
        response_mode: str,
    ) -> ModelGenerationResult:
        del system_fingerprint
        return _build_result(
            response,
            content=content,
            usage_obj=usage_obj,
            provider_request_id=provider_request_id,
            returned_model=returned_model,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            attempts=attempts,
            api_key=api_key,
            response_mode=response_mode,
            adapter_name="anthropic_messages",
        )
