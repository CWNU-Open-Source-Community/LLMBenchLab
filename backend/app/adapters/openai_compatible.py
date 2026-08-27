"""Adapter for OpenAI-compatible Chat Completions endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

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

from .base import AdapterError, GenerationConfig, Message, ModelAdapter, ModelGenerationResult

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
_RESPONSE_READ_CHUNK_BYTES = 64 * 1024


class _ResponseBodyTooLarge(RuntimeError):
    """Internal bounded-read signal that never stores response content."""

    def __init__(self, *, limit: int, status_code: int) -> None:
        super().__init__("upstream response exceeded its byte limit")
        self.limit = limit
        self.status_code = status_code


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
    return value


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
        api_key_env: str,
        *,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        write_timeout_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS,
        pool_timeout_seconds: float = DEFAULT_POOL_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_base_seconds: float = DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
        retry_backoff_cap_seconds: float = DEFAULT_RETRY_BACKOFF_CAP_SECONDS,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not isinstance(remote_model_name, str):
            raise ValueError("base_url and remote_model_name must be strings")
        if not isinstance(api_key_env, str):
            raise ValueError("api_key_env must be a string")
        self.base_url = _validated_base_url(base_url)
        self.remote_model_name = remote_model_name
        self.api_key_env = api_key_env
        self.max_retries = max_retries
        self.retry_backoff_base_seconds = retry_backoff_base_seconds
        self.retry_backoff_cap_seconds = retry_backoff_cap_seconds
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep or asyncio.sleep

        if not self.base_url or not self.remote_model_name or not self.api_key_env:
            raise ValueError("base_url, remote_model_name, and api_key_env are required")
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
    ) -> ModelGenerationResult:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise AdapterError(
                "missing_api_key",
                f"API key environment variable {self.api_key_env!r} is not set.",
                status_code=None,
            )

        payload = self._build_payload(messages, generation_config)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        }
        started = time.perf_counter()
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
            self._client = client
        for attempt in range(1, self.max_retries + 2):
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
                            status_code=response.status_code,
                            attempts=attempt,
                        )
                    response_limit = (
                        MAX_CHAT_SUCCESS_RESPONSE_BYTES
                        if 200 <= response.status_code < 300
                        else MAX_CHAT_ERROR_RESPONSE_BYTES
                    )
                    response_body = await self._read_response_body(
                        response,
                        limit=response_limit,
                    )
            except _ResponseBodyTooLarge as exc:
                raise AdapterError(
                    "provider_response_too_large",
                    f"Upstream response exceeded the {exc.limit}-byte safety limit.",
                    status_code=exc.status_code,
                    attempts=attempt,
                ) from exc
            except httpx.TransportError as exc:
                error_type = self._transport_error_type(exc)
                safe_message = sanitize_error_message(exc, api_key)
                if attempt <= self.max_retries:
                    await self._backoff(attempt)
                    continue
                raise AdapterError(
                    error_type,
                    f"OpenAI-compatible request failed: {safe_message}",
                    retryable=True,
                    attempts=attempt,
                ) from exc

            if not 200 <= response.status_code < 300:
                error_type, retryable = self._status_error_type(response.status_code)
                safe_detail = self._safe_response_error(response, response_body, api_key)
                if retryable and attempt <= self.max_retries:
                    await self._backoff(attempt)
                    continue
                raise AdapterError(
                    error_type,
                    f"Upstream returned HTTP {response.status_code}: {safe_detail}",
                    retryable=retryable,
                    status_code=response.status_code,
                    attempts=attempt,
                )

            return self._parse_success_response(
                response,
                response_body,
                latency_ms=(time.perf_counter() - started) * 1000,
                attempts=attempt,
                api_key=api_key,
            )

        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        """Close the lazily-created connection pool owned by this adapter."""

        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

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
        async for chunk in response.aiter_raw(chunk_size=_RESPONSE_READ_CHUNK_BYTES):
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
                status_code=response.status_code,
                attempts=attempts,
            ) from exc
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterError(
                "invalid_provider_response",
                "Upstream response did not contain choices[0].message.content.",
                status_code=response.status_code,
                attempts=attempts,
            ) from exc
        if not isinstance(content, str):
            raise AdapterError(
                "invalid_provider_response",
                "Upstream response content was not text.",
                status_code=response.status_code,
                attempts=attempts,
            )
        if not content.strip():
            raise AdapterError(
                "empty_response",
                "Upstream returned an empty model response.",
                status_code=response.status_code,
                attempts=attempts,
            )
        content = content.replace(api_key, "[REDACTED]")

        usage_obj = body.get("usage") if isinstance(body, Mapping) else None
        raw_usage = (
            _redact_json_secret(dict(usage_obj), api_key)
            if isinstance(usage_obj, Mapping)
            else None
        )
        input_tokens = OpenAICompatibleAdapter._usage_int(usage_obj, "prompt_tokens")
        output_tokens = OpenAICompatibleAdapter._usage_int(usage_obj, "completion_tokens")
        provider_request_id = body.get("id") if isinstance(body, Mapping) else None
        if provider_request_id is None:
            provider_request_id = response.headers.get("x-request-id") or response.headers.get(
                "request-id"
            )
        finish_reason = choice.get("finish_reason") if isinstance(choice, Mapping) else None
        returned_model = body.get("model") if isinstance(body, Mapping) else None
        system_fingerprint = body.get("system_fingerprint") if isinstance(body, Mapping) else None
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
