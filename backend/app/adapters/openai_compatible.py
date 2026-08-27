"""Adapter for OpenAI-compatible Chat Completions endpoints."""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import math
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.core.constants import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_POOL_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
    DEFAULT_RETRY_BACKOFF_CAP_SECONDS,
    DEFAULT_WRITE_TIMEOUT_SECONDS,
    RETRYABLE_PROVIDER_STATUS_CODES,
)
from app.provider_attempts import ProviderAttemptStateUnknown

from .base import (
    AdapterError,
    GenerationConfig,
    Message,
    ModelAdapter,
    ModelGenerationResult,
    ProviderAttemptContext,
    ProviderAttemptController,
    ProviderAttemptDisposition,
    ProviderAttemptOutcome,
    _finish_provider_attempt,
    _finish_provider_attempt_after_cancellation,
    _mark_provider_attempt_send_started,
    _reserve_provider_attempt,
)

_RETRYABLE_STATUS_CODES = frozenset(RETRYABLE_PROVIDER_STATUS_CODES)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?!\[REDACTED\])(?:bearer\s+)?[^\s,;\]\}]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_FIELD_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|secret)(\s*[:=]\s*)['\"]?[^\s,'\"}\]]+"
)
MAX_CHAT_SUCCESS_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CHAT_ERROR_RESPONSE_BYTES = 64 * 1024
MAX_CHAT_STREAM_WIRE_BYTES = 64 * 1024 * 1024
MAX_CHAT_STREAM_EVENT_BYTES = 1024 * 1024
_RESPONSE_READ_CHUNK_BYTES = 64 * 1024
_HTTP_ADAPTER_ERROR_TYPES = frozenset(
    {
        "authentication_error",
        "rate_limited",
        "provider_5xx",
        "provider_4xx",
        "provider_http_error",
    }
)


class _ResponseBodyTooLarge(RuntimeError):
    """Internal bounded-read signal that never stores response content."""

    def __init__(self, *, limit: int, status_code: int) -> None:
        super().__init__("upstream response exceeded its byte limit")
        self.limit = limit
        self.status_code = status_code


class _SSEAccumulator:
    """Request-local normalized state for one Chat Completions SSE response."""

    def __init__(self, *, status_code: int, attempts: int, api_key: str) -> None:
        self.status_code = status_code
        self.attempts = attempts
        self.api_key = api_key
        self._content = io.StringIO()
        self.content_bytes = 0
        self.finish_reason: str | None = None
        self.usage: Mapping[str, Any] | None = None
        self.provider_request_id: str | None = None
        self.returned_model: str | None = None
        self.system_fingerprint: str | None = None
        self.done = False

    @property
    def content(self) -> str:
        return self._content.getvalue()

    def consume_event(self, raw_event: bytes) -> None:
        """Consume one decoded SSE ``data`` event without retaining raw bytes."""

        try:
            event_text = raw_event.decode("utf-8")
        except UnicodeError as exc:
            raise self._invalid("Upstream SSE data was not valid UTF-8.") from exc
        if event_text.strip() == "[DONE]":
            self.done = True
            return
        try:
            body = json.loads(event_text)
        except ValueError as exc:
            raise self._invalid("Upstream SSE data was not valid JSON.") from exc
        if not isinstance(body, Mapping):
            raise self._invalid("Upstream SSE data was not a JSON object.")

        error = body.get("error")
        if error is not None:
            detail: object = "Provider reported an error while streaming."
            if isinstance(error, Mapping):
                detail = error.get("message") or error.get("type") or detail
            else:
                detail = error
            raise AdapterError(
                "provider_stream_error",
                sanitize_error_message(f"Upstream SSE error: {detail}", self.api_key),
                status_code=_safe_status_code(self.status_code, self.api_key),
                attempts=self.attempts,
            )

        self._capture_stable_string(body, "id", "provider_request_id")
        self._capture_stable_string(body, "model", "returned_model")
        self._capture_stable_string(body, "system_fingerprint", "system_fingerprint")

        if body.get("usage") is not None:
            usage = body["usage"]
            if not isinstance(usage, Mapping):
                raise self._invalid("Upstream SSE usage was not an object.")
            # Streaming usage is cumulative. Keep the latest complete object;
            # summing events would double-count Providers that repeat it.
            self.usage = dict(usage)

        choices = body.get("choices")
        if choices is None:
            return
        if not isinstance(choices, list):
            raise self._invalid("Upstream SSE choices was not an array.")
        if not choices:
            return

        choice = self._select_choice(choices)
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            if not isinstance(finish_reason, str):
                raise self._invalid("Upstream SSE finish_reason was not text.")
            if self.finish_reason is not None and self.finish_reason != finish_reason:
                raise self._invalid("Upstream SSE returned conflicting finish reasons.")
            self.finish_reason = finish_reason

        delta = choice.get("delta")
        if delta is None:
            return
        if not isinstance(delta, Mapping):
            raise self._invalid("Upstream SSE delta was not an object.")
        content = delta.get("content")
        if content is None:
            return
        if not isinstance(content, str):
            raise self._invalid("Upstream SSE content delta was not text.")
        try:
            encoded_size = len(content.encode("utf-8"))
        except UnicodeError as exc:
            raise self._invalid("Upstream SSE content was not valid Unicode text.") from exc
        if self.content_bytes + encoded_size > MAX_CHAT_SUCCESS_RESPONSE_BYTES:
            raise _ResponseBodyTooLarge(
                limit=MAX_CHAT_SUCCESS_RESPONSE_BYTES,
                status_code=self.status_code,
            )
        self.content_bytes += encoded_size
        self._content.write(content)

    def _capture_stable_string(
        self,
        body: Mapping[str, Any],
        response_key: str,
        attribute: str,
    ) -> None:
        value = body.get(response_key)
        if value is None:
            return
        if not isinstance(value, str):
            raise self._invalid(f"Upstream SSE {response_key} was not text.")
        existing = getattr(self, attribute)
        if existing is not None and existing != value:
            raise self._invalid(f"Upstream SSE returned conflicting {response_key} values.")
        setattr(self, attribute, value)

    def _select_choice(self, choices: list[Any]) -> Mapping[str, Any]:
        for candidate in choices:
            if isinstance(candidate, Mapping) and candidate.get("index") == 0:
                return candidate
        first = choices[0]
        if not isinstance(first, Mapping):
            raise self._invalid("Upstream SSE choices[0] was not an object.")
        return first

    def _invalid(self, message: str) -> AdapterError:
        return AdapterError(
            "invalid_provider_stream",
            message,
            status_code=_safe_status_code(self.status_code, self.api_key),
            attempts=self.attempts,
        )


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validated_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("base_url must be an absolute HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain embedded credentials")
    if parsed.query:
        raise ValueError("base_url must not contain query parameters")
    if parsed.fragment:
        raise ValueError("base_url must not contain a URL fragment")
    if parsed.scheme == "http" and not _is_loopback_host(hostname):
        raise ValueError("plain HTTP base_url is allowed only for loopback hosts")
    return normalized


def sanitize_error_message(message: object, *secrets: str) -> str:
    """Return a bounded diagnostic string with common credentials removed."""

    sanitized = str(message).replace("\r", " ").replace("\n", " ")
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    sanitized = _BEARER_RE.sub("[REDACTED]", sanitized)
    sanitized = _AUTHORIZATION_RE.sub(r"\1[REDACTED]", sanitized)
    sanitized = _SECRET_FIELD_RE.sub(r"\1\2[REDACTED]", sanitized)
    sanitized = " ".join(sanitized.split())
    return sanitized[:500] or "Upstream request failed."


def _redact_json_secret(value: Any, secret: str) -> Any:
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [_redact_json_secret(item, secret) for item in value]
    if isinstance(value, Mapping):
        return {
            (
                key.replace(secret, "[REDACTED]") if isinstance(key, str) else key
            ): _redact_json_secret(item, secret)
            for key, item in value.items()
        }
    if value is None or isinstance(value, (bool, int, float)):
        rendered = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        if secret in rendered:
            return "[REDACTED]"
    return value


def _safe_status_code(status_code: int, secret: str) -> int | None:
    """Prevent an all-numeric credential from escaping through status metadata."""

    return None if secret and secret in str(status_code) else status_code


class OpenAICompatibleAdapter(ModelAdapter):
    """Call an OpenAI-compatible ``/chat/completions`` HTTP endpoint.

    A caller may inject an ``httpx.AsyncClient`` and sleep coroutine for fully
    offline tests.  Injected clients are owned by the caller and are never
    closed by this adapter.
    """

    def __init__(
        self,
        base_url: str,
        remote_model_name: str,
        api_key_env: str | None = None,
        *,
        api_key: SecretStr | str | None = None,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        write_timeout_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS,
        pool_timeout_seconds: float = DEFAULT_POOL_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_base_seconds: float = DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
        retry_backoff_cap_seconds: float = DEFAULT_RETRY_BACKOFF_CAP_SECONDS,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        attempt_controller: ProviderAttemptController | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not isinstance(remote_model_name, str):
            raise ValueError("base_url and remote_model_name must be strings")
        if api_key_env is not None and not isinstance(api_key_env, str):
            raise ValueError("api_key_env must be a string or null")
        if api_key is not None and not isinstance(api_key, (SecretStr, str)):
            raise ValueError("api_key must be a secret string or null")
        self.base_url = _validated_base_url(base_url)
        self.remote_model_name = remote_model_name
        self.api_key_env = api_key_env
        self._api_key = SecretStr(api_key) if isinstance(api_key, str) else api_key
        self.max_retries = max_retries
        self.retry_backoff_base_seconds = retry_backoff_base_seconds
        self.retry_backoff_cap_seconds = retry_backoff_cap_seconds
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep or asyncio.sleep
        self._attempt_controller = attempt_controller

        if not self.base_url or not self.remote_model_name:
            raise ValueError("base_url and remote_model_name are required")
        if bool(self.api_key_env) == bool(self._api_key):
            raise ValueError("exactly one of api_key_env or api_key is required")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        numeric_settings = {
            "connect_timeout_seconds": connect_timeout_seconds,
            "read_timeout_seconds": read_timeout_seconds,
            "write_timeout_seconds": write_timeout_seconds,
            "pool_timeout_seconds": pool_timeout_seconds,
            "retry_backoff_base_seconds": retry_backoff_base_seconds,
            "retry_backoff_cap_seconds": retry_backoff_cap_seconds,
        }
        for name, value in numeric_settings.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative number")
        if (
            min(
                connect_timeout_seconds,
                read_timeout_seconds,
                write_timeout_seconds,
                pool_timeout_seconds,
            )
            <= 0
        ):
            raise ValueError("HTTP timeout values must be greater than zero")

        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=write_timeout_seconds,
            pool=pool_timeout_seconds,
        )

    @property
    def chat_completions_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    async def generate(
        self,
        messages: Sequence[Message],
        generation_config: GenerationConfig,
        *,
        attempt_context: ProviderAttemptContext | None = None,
    ) -> ModelGenerationResult:
        api_key = (
            self._api_key.get_secret_value()
            if self._api_key is not None
            else os.environ.get(self.api_key_env or "")
        )
        if not api_key:
            if self._api_key is not None:
                source = "stored API key"
            else:
                source = f"API key environment variable {self.api_key_env!r}"
            raise AdapterError(
                "missing_api_key",
                f"{source} is not set.",
                status_code=None,
            )

        payload = self._build_payload(messages, generation_config)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        }
        first_attempt = self._first_provider_attempt(attempt_context)
        started = time.perf_counter()
        client = self._client
        if client is None:
            client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            )
            self._client = client
        for attempt in range(first_attempt, self.max_retries + 2):
            permit = await _reserve_provider_attempt(
                self._attempt_controller,
                attempt_context,
                provider_attempt=attempt,
            )
            try:
                await _mark_provider_attempt_send_started(self._attempt_controller, permit)
            except asyncio.CancelledError as exc:
                exc.add_note("Provider send-start outcome is unknown; reconciliation is required.")
                raise
            except ProviderAttemptStateUnknown:
                raise
            except BaseException:
                await _finish_provider_attempt(
                    self._attempt_controller,
                    permit,
                    disposition=ProviderAttemptDisposition.RELEASED_PRE_SEND,
                    outcome=ProviderAttemptOutcome.MARK_SEND_FAILED,
                )
                raise

            stream_result: _SSEAccumulator | None = None
            response_body: bytes | None = None
            terminal_transport_error: AdapterError | None = None
            try:
                async with client.stream(
                    "POST",
                    self.chat_completions_url,
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                ) as response:
                    content_encoding = response.headers.get("content-encoding", "identity")
                    if content_encoding.strip().lower() not in {"", "identity"}:
                        raise AdapterError(
                            "unsupported_provider_response_encoding",
                            "Upstream returned a compressed response despite the identity-only "
                            "request.",
                            status_code=_safe_status_code(response.status_code, api_key),
                            attempts=attempt,
                        )
                    if not 200 <= response.status_code < 300:
                        response_body = await self._read_response_body(
                            response,
                            limit=MAX_CHAT_ERROR_RESPONSE_BYTES,
                        )
                    elif self._is_event_stream(response):
                        stream_result = await self._consume_sse_response(
                            response,
                            attempts=attempt,
                            api_key=api_key,
                        )
                    else:
                        # Some compatible Providers ignore ``stream=true`` and
                        # return the traditional JSON success body.
                        response_body = await self._read_response_body(
                            response,
                            limit=MAX_CHAT_SUCCESS_RESPONSE_BYTES,
                        )

                if not 200 <= response.status_code < 300:
                    assert response_body is not None
                    error_type, retryable = self._status_error_type(response.status_code)
                    safe_detail = self._safe_response_error(response, response_body, api_key)
                    raise AdapterError(
                        error_type,
                        sanitize_error_message(
                            f"Upstream returned HTTP {response.status_code}: {safe_detail}",
                            api_key,
                        ),
                        retryable=retryable,
                        status_code=_safe_status_code(response.status_code, api_key),
                        attempts=attempt,
                    )

                latency_ms = (time.perf_counter() - started) * 1000
                if stream_result is not None:
                    result = self._build_generation_result(
                        response,
                        content=stream_result.content,
                        finish_reason=stream_result.finish_reason,
                        usage_obj=stream_result.usage,
                        provider_request_id=stream_result.provider_request_id,
                        returned_model=stream_result.returned_model,
                        system_fingerprint=stream_result.system_fingerprint,
                        latency_ms=latency_ms,
                        attempts=attempt,
                        api_key=api_key,
                        response_mode="sse",
                    )
                else:
                    assert response_body is not None
                    result = self._parse_success_response(
                        response,
                        response_body,
                        latency_ms=latency_ms,
                        attempts=attempt,
                        api_key=api_key,
                    )
            except asyncio.CancelledError as exc:
                confirmed = await _finish_provider_attempt_after_cancellation(
                    self._attempt_controller,
                    permit,
                    disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
                    outcome=ProviderAttemptOutcome.CANCELLED,
                )
                if not confirmed:
                    exc.add_note(
                        "Provider attempt settlement was not confirmed; reconciliation is required."
                    )
                raise
            except _ResponseBodyTooLarge as exc:
                await _finish_provider_attempt(
                    self._attempt_controller,
                    permit,
                    disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
                    outcome=ProviderAttemptOutcome.PROVIDER_RESPONSE_ERROR,
                )
                raise AdapterError(
                    "provider_response_too_large",
                    sanitize_error_message(
                        f"Upstream response exceeded the {exc.limit}-byte safety limit.",
                        api_key,
                    ),
                    status_code=_safe_status_code(exc.status_code, api_key),
                    attempts=attempt,
                ) from exc
            except httpx.TransportError as exc:
                await _finish_provider_attempt(
                    self._attempt_controller,
                    permit,
                    disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
                    outcome=ProviderAttemptOutcome.TRANSPORT_ERROR,
                )
                error_type = self._transport_error_type(exc)
                safe_message = sanitize_error_message(exc, api_key)
                if attempt <= self.max_retries:
                    await self._backoff(attempt)
                    continue
                terminal_transport_error = AdapterError(
                    error_type,
                    f"OpenAI-compatible request failed: {safe_message}",
                    retryable=True,
                    attempts=attempt,
                )
            except AdapterError as exc:
                outcome = (
                    ProviderAttemptOutcome.HTTP_ERROR
                    if exc.error_type in _HTTP_ADAPTER_ERROR_TYPES
                    else ProviderAttemptOutcome.PROVIDER_RESPONSE_ERROR
                )
                await _finish_provider_attempt(
                    self._attempt_controller,
                    permit,
                    disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
                    outcome=outcome,
                )
                if exc.retryable and attempt <= self.max_retries:
                    await self._backoff(attempt)
                    continue
                raise
            except BaseException:
                await _finish_provider_attempt(
                    self._attempt_controller,
                    permit,
                    disposition=ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
                    outcome=ProviderAttemptOutcome.UNEXPECTED_ERROR,
                )
                raise

            # Raise only after leaving the TransportError handler.  httpx transport
            # exceptions retain their request (including Authorization), and Python
            # otherwise attaches that exception as both cause/context to AdapterError.
            if terminal_transport_error is not None:
                raise terminal_transport_error

            usage_complete = result.input_tokens is not None and result.output_tokens is not None
            await _finish_provider_attempt(
                self._attempt_controller,
                permit,
                disposition=(
                    ProviderAttemptDisposition.SETTLED_ACTUAL
                    if usage_complete
                    else ProviderAttemptDisposition.SETTLED_CONSERVATIVE
                ),
                outcome=(
                    ProviderAttemptOutcome.SUCCEEDED
                    if usage_complete
                    else ProviderAttemptOutcome.USAGE_INCOMPLETE
                ),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
            return result

        raise AssertionError("unreachable")

    def _first_provider_attempt(self, context: ProviderAttemptContext | None) -> int:
        if self._attempt_controller is None:
            if context is not None:
                raise ValueError("attempt_context requires an attempt_controller")
            return 1
        if context is None:
            raise ValueError("attempt_context is required when attempt_controller is configured")
        first_attempt = context.next_provider_attempt
        if (
            isinstance(first_attempt, bool)
            or not isinstance(first_attempt, int)
            or not 1 <= first_attempt <= self.max_retries + 1
        ):
            raise ValueError(
                "attempt_context.next_provider_attempt exceeds the configured retry policy"
            )
        return first_attempt

    async def aclose(self) -> None:
        """Close the lazily-created connection pool owned by this adapter."""

        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        self._api_key = None

    def _build_payload(
        self,
        messages: Sequence[Message],
        generation_config: GenerationConfig,
    ) -> dict[str, Any]:
        prepared_messages: list[dict[str, Any]] = []
        system_prompt = generation_config.get("system_prompt")
        if system_prompt is not None and str(system_prompt).strip():
            prepared_messages.append({"role": "system", "content": str(system_prompt)})
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                raise AdapterError(
                    "invalid_request",
                    f"Message at index {index} must be an object.",
                )
            role = message.get("role")
            if not isinstance(role, str) or not role:
                raise AdapterError(
                    "invalid_request",
                    f"Message at index {index} requires a role.",
                )
            prepared_messages.append(dict(message))
        if not prepared_messages:
            raise AdapterError("invalid_request", "At least one message is required.")

        payload: dict[str, Any] = {
            "model": self.remote_model_name,
            "messages": prepared_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        for key in ("temperature", "top_p", "max_tokens", "seed"):
            value = generation_config.get(key)
            if value is not None:
                payload[key] = value
        return payload

    async def _backoff(self, failed_attempt: int) -> None:
        delay = min(
            self.retry_backoff_base_seconds * (2 ** (failed_attempt - 1)),
            self.retry_backoff_cap_seconds,
        )
        if delay > 0:
            await self._sleep(delay)

    @staticmethod
    def _transport_error_type(exc: httpx.TransportError) -> str:
        if isinstance(exc, httpx.ConnectTimeout):
            return "connect_timeout"
        if isinstance(exc, httpx.ReadTimeout):
            return "read_timeout"
        if isinstance(exc, (httpx.WriteTimeout, httpx.PoolTimeout)):
            return "network_timeout"
        return "network_error"

    @staticmethod
    def _status_error_type(status_code: int) -> tuple[str, bool]:
        if status_code in {401, 403}:
            return "authentication_error", False
        if status_code == 429:
            return "rate_limited", True
        if 500 <= status_code <= 599:
            return "provider_5xx", status_code in _RETRYABLE_STATUS_CODES
        if 400 <= status_code <= 499:
            return "provider_4xx", status_code in _RETRYABLE_STATUS_CODES
        return "provider_http_error", False

    @staticmethod
    def _is_event_stream(response: httpx.Response) -> bool:
        content_type = response.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        return media_type == "text/event-stream"

    @staticmethod
    async def _consume_sse_response(
        response: httpx.Response,
        *,
        attempts: int,
        api_key: str,
    ) -> _SSEAccumulator:
        """Incrementally consume one bounded Server-Sent Events response."""

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

        accumulator = _SSEAccumulator(
            status_code=response.status_code,
            attempts=attempts,
            api_key=api_key,
        )
        line_buffer = bytearray()
        data_lines: list[bytes] = []
        event_bytes = 0
        wire_bytes = 0
        first_line = True

        def process_line(line: bytes) -> None:
            nonlocal event_bytes, first_line
            raw_line_size = len(line)
            if first_line:
                first_line = False
                if line.startswith(b"\xef\xbb\xbf"):
                    line = line[3:]
            if not line:
                if data_lines:
                    accumulator.consume_event(b"\n".join(data_lines))
                    data_lines.clear()
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
            if field != b"data":
                return
            if not separator:
                value = b""
            elif value.startswith(b" "):
                value = value[1:]
            data_lines.append(value)

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
        if data_lines:
            accumulator.consume_event(b"\n".join(data_lines))
        if not accumulator.done:
            raise AdapterError(
                "incomplete_provider_stream",
                "Upstream SSE response ended before the [DONE] marker.",
                status_code=_safe_status_code(response.status_code, api_key),
                attempts=attempts,
            )
        return accumulator

    @staticmethod
    def _pop_sse_line(buffer: bytearray, *, at_eof: bool = False) -> bytes | None:
        """Pop one SSE line, accepting LF, CRLF, and standalone CR endings."""

        for index, value in enumerate(buffer):
            if value == 10:  # LF
                line = bytes(buffer[:index])
                del buffer[: index + 1]
                return line
            if value != 13:  # CR
                continue
            if index + 1 == len(buffer) and not at_eof:
                return None
            ending_size = 2 if index + 1 < len(buffer) and buffer[index + 1] == 10 else 1
            line = bytes(buffer[:index])
            del buffer[: index + ending_size]
            return line
        if at_eof and buffer:
            line = bytes(buffer)
            buffer.clear()
            return line
        return None

    @staticmethod
    async def _read_response_body(response: httpx.Response, *, limit: int) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = None
            if declared_length is not None and declared_length > limit:
                raise _ResponseBodyTooLarge(
                    limit=limit,
                    status_code=response.status_code,
                )

        # Mock/custom transports may legally return an already-buffered response.
        # The standard network transport remains on the streaming path below.
        if response.is_stream_consumed:
            buffered = response.content
            if len(buffered) > limit:
                raise _ResponseBodyTooLarge(
                    limit=limit,
                    status_code=response.status_code,
                )
            return buffered

        content = bytearray()
        async with aclosing(response.aiter_raw(chunk_size=_RESPONSE_READ_CHUNK_BYTES)) as chunks:
            async for chunk in chunks:
                if len(content) + len(chunk) > limit:
                    raise _ResponseBodyTooLarge(
                        limit=limit,
                        status_code=response.status_code,
                    )
                content.extend(chunk)
        return bytes(content)

    @staticmethod
    def _safe_response_error(
        response: httpx.Response,
        response_body: bytes,
        api_key: str,
    ) -> str:
        detail: object = response.reason_phrase or "Request failed"
        try:
            body = json.loads(response_body)
        except (ValueError, UnicodeError):
            body = None
        if isinstance(body, Mapping):
            error = body.get("error")
            if isinstance(error, Mapping):
                detail = error.get("message") or error.get("type") or detail
            elif error is not None:
                detail = error
            elif body.get("message") is not None:
                detail = body["message"]
        return sanitize_error_message(detail, api_key)

    @staticmethod
    def _parse_success_response(
        response: httpx.Response,
        response_body: bytes,
        *,
        latency_ms: float,
        attempts: int,
        api_key: str,
    ) -> ModelGenerationResult:
        try:
            body = json.loads(response_body)
        except (ValueError, UnicodeError) as exc:
            raise AdapterError(
                "invalid_provider_response",
                "Upstream returned a non-JSON success response.",
                status_code=_safe_status_code(response.status_code, api_key),
                attempts=attempts,
            ) from exc
        try:
            choice = body["choices"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterError(
                "invalid_provider_response",
                "Upstream response did not contain choices[0].",
                status_code=_safe_status_code(response.status_code, api_key),
                attempts=attempts,
            ) from exc
        finish_reason = choice.get("finish_reason") if isinstance(choice, Mapping) else None
        try:
            content = choice["message"]["content"]
        except (KeyError, TypeError) as exc:
            if finish_reason == "length":
                raise AdapterError(
                    "output_truncated",
                    "Upstream exhausted the output token budget before returning text. "
                    "Increase max_tokens or let the Provider choose its default.",
                    status_code=_safe_status_code(response.status_code, api_key),
                    attempts=attempts,
                ) from exc
            raise AdapterError(
                "invalid_provider_response",
                "Upstream response did not contain choices[0].message.content.",
                status_code=_safe_status_code(response.status_code, api_key),
                attempts=attempts,
            ) from exc
        usage_obj = body.get("usage") if isinstance(body, Mapping) else None
        provider_request_id = body.get("id") if isinstance(body, Mapping) else None
        returned_model = body.get("model") if isinstance(body, Mapping) else None
        system_fingerprint = body.get("system_fingerprint") if isinstance(body, Mapping) else None
        return OpenAICompatibleAdapter._build_generation_result(
            response,
            content=content,
            finish_reason=finish_reason,
            usage_obj=usage_obj,
            provider_request_id=provider_request_id,
            returned_model=returned_model,
            system_fingerprint=system_fingerprint,
            latency_ms=latency_ms,
            attempts=attempts,
            api_key=api_key,
            response_mode="json",
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
        output_budget_exhausted = finish_reason == "length"
        if not isinstance(content, str):
            if output_budget_exhausted:
                raise AdapterError(
                    "output_truncated",
                    "Upstream exhausted the output token budget before returning text. "
                    "Increase max_tokens or let the Provider choose its default.",
                    status_code=_safe_status_code(response.status_code, api_key),
                    attempts=attempts,
                )
            raise AdapterError(
                "invalid_provider_response",
                "Upstream response content was not text.",
                status_code=_safe_status_code(response.status_code, api_key),
                attempts=attempts,
            )
        if not content.strip():
            if output_budget_exhausted:
                raise AdapterError(
                    "output_truncated",
                    "Upstream exhausted the output token budget before returning text. "
                    "Increase max_tokens or let the Provider choose its default.",
                    status_code=_safe_status_code(response.status_code, api_key),
                    attempts=attempts,
                )
            raise AdapterError(
                "empty_response",
                "Upstream returned an empty model response.",
                status_code=_safe_status_code(response.status_code, api_key),
                attempts=attempts,
            )
        content = content.replace(api_key, "[REDACTED]")

        raw_usage = (
            _redact_json_secret(dict(usage_obj), api_key)
            if isinstance(usage_obj, Mapping)
            else None
        )
        input_tokens = OpenAICompatibleAdapter._usage_int(raw_usage, "prompt_tokens")
        output_tokens = OpenAICompatibleAdapter._usage_int(raw_usage, "completion_tokens")
        if provider_request_id is None:
            provider_request_id = response.headers.get("x-request-id") or response.headers.get(
                "request-id"
            )
        return ModelGenerationResult(
            text=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=max(0.0, latency_ms),
            provider_request_id=(
                None
                if provider_request_id is None
                else sanitize_error_message(
                    provider_request_id,
                    api_key,
                )
            ),
            raw_usage=raw_usage,
            metadata={
                "adapter": "openai_compatible",
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
                "system_fingerprint": (
                    sanitize_error_message(system_fingerprint, api_key)
                    if isinstance(system_fingerprint, str)
                    else None
                ),
            },
        )

    @staticmethod
    def _usage_int(usage: object, key: str) -> int | None:
        if not isinstance(usage, Mapping):
            return None
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value
