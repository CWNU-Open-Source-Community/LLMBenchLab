"""Secret-safe discovery and a minimal billed Chat Completions canary.

These helpers are intentionally used only by the trusted-local CLI.  The API
key is supplied by the caller and is never returned, logged, or persisted.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.adapters import AdapterError, OpenAICompatibleAdapter, sanitize_error_message
from app.evaluators import get_evaluator

MAX_DISCOVERY_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DISCOVERED_MODELS = 10_000
_DISCOVERY_READ_CHUNK_BYTES = 64 * 1024
_MODEL_ID_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")


class ProviderPreflightError(RuntimeError):
    """A bounded, already-sanitized error safe to show to a local operator."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ModelDiscoveryResult:
    models: tuple[str, ...]
    request_id: str | None


@dataclass(frozen=True, slots=True)
class CanaryResult:
    model: str
    returned_model: str | None
    system_fingerprint: str | None
    finish_reason: str | None
    provider_request_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: float
    attempts: int


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validated_base_url(base_url: str) -> tuple[str, str, str, str, str]:
    if not isinstance(base_url, str):
        raise ProviderPreflightError("invalid_base_url", "Base URL must be a string.")
    normalized = base_url.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderPreflightError(
            "invalid_base_url", "Base URL must be an absolute HTTP(S) URL."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderPreflightError(
            "invalid_base_url",
            "Base URL must not contain credentials, query parameters, or a fragment.",
        )
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ProviderPreflightError(
            "invalid_base_url",
            "Plain HTTP is allowed only for loopback Provider hosts; use HTTPS otherwise.",
        )
    return parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""


def models_url(base_url: str) -> str:
    """Return the sibling ``/models`` URL for a compatible endpoint."""

    scheme, netloc, path, query, fragment = _validated_base_url(base_url)
    suffix = "/chat/completions"
    if path.endswith(suffix):
        path = path[: -len(suffix)]
    return urlunsplit((scheme, netloc, f"{path}/models", query, fragment))


def _safe_request_id(response: httpx.Response, api_key: str) -> str | None:
    for header in ("x-request-id", "request-id", "x-amzn-requestid"):
        value = response.headers.get(header)
        if value:
            return sanitize_error_message(value, api_key)[:128]
    return None


def _safe_error_detail(response: httpx.Response, api_key: str, content: bytes) -> str:
    detail: object = response.reason_phrase or "Request failed"
    try:
        body = json.loads(content)
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


async def discover_models(
    base_url: str,
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> ModelDiscoveryResult:
    """Authenticate against ``GET /models`` and return bounded model IDs."""

    if not api_key:
        raise ProviderPreflightError("missing_api_key", "API key is empty.")
    endpoint = models_url(base_url)
    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5, read=30, write=10, pool=5),
        follow_redirects=False,
        trust_env=False,
    )
    try:
        try:
            async with active_client.stream(
                "GET",
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                content_encoding = response.headers.get("content-encoding", "identity")
                if content_encoding.strip().lower() not in {"", "identity"}:
                    raise ProviderPreflightError(
                        "unsupported_model_discovery_response_encoding",
                        "Model discovery returned a compressed response despite the "
                        "identity-only request.",
                        status_code=response.status_code,
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        declared_length = None
                    if (
                        declared_length is not None
                        and declared_length > MAX_DISCOVERY_RESPONSE_BYTES
                    ):
                        raise ProviderPreflightError(
                            "model_discovery_response_too_large",
                            "Model discovery response exceeds the 2 MiB safety limit.",
                        )
                content = bytearray()
                if response.is_stream_consumed:
                    buffered = response.content
                    if len(buffered) > MAX_DISCOVERY_RESPONSE_BYTES:
                        raise ProviderPreflightError(
                            "model_discovery_response_too_large",
                            "Model discovery response exceeds the 2 MiB safety limit.",
                        )
                    content.extend(buffered)
                else:
                    async for chunk in response.aiter_raw(chunk_size=_DISCOVERY_READ_CHUNK_BYTES):
                        if len(content) + len(chunk) > MAX_DISCOVERY_RESPONSE_BYTES:
                            raise ProviderPreflightError(
                                "model_discovery_response_too_large",
                                "Model discovery response exceeds the 2 MiB safety limit.",
                            )
                        content.extend(chunk)
                response_content = bytes(content)
        except ProviderPreflightError:
            raise
        except httpx.TransportError as exc:
            raise ProviderPreflightError(
                "model_discovery_network_error",
                "Model discovery request failed: " + sanitize_error_message(exc, api_key),
            ) from exc
        if response.status_code >= 400:
            code = (
                "model_discovery_authentication_error"
                if response.status_code in {401, 403}
                else "model_discovery_unsupported"
                if response.status_code in {404, 405}
                else "model_discovery_http_error"
            )
            raise ProviderPreflightError(
                code,
                f"Model discovery returned HTTP {response.status_code}: "
                f"{_safe_error_detail(response, api_key, response_content)}",
                status_code=response.status_code,
            )
        try:
            body = json.loads(response_content)
        except (ValueError, UnicodeError) as exc:
            raise ProviderPreflightError(
                "invalid_model_discovery_response",
                "Model discovery returned a non-JSON response.",
            ) from exc
        if not isinstance(body, Mapping) or not isinstance(body.get("data"), list):
            raise ProviderPreflightError(
                "invalid_model_discovery_response",
                "Model discovery response must contain a data array.",
            )
        raw_models = body["data"]
        if len(raw_models) > MAX_DISCOVERED_MODELS:
            raise ProviderPreflightError(
                "too_many_discovered_models",
                f"Model discovery returned more than {MAX_DISCOVERED_MODELS} entries.",
            )
        model_ids: set[str] = set()
        for item in raw_models:
            if not isinstance(item, Mapping):
                continue
            model_id = item.get("id")
            if isinstance(model_id, str) and _MODEL_ID_RE.fullmatch(model_id):
                if api_key in model_id:
                    raise ProviderPreflightError(
                        "model_discovery_secret_reflection",
                        "Model discovery returned an unsafe model ID.",
                    )
                model_ids.add(model_id)
        if not model_ids:
            raise ProviderPreflightError(
                "no_models_discovered", "Model discovery returned no usable model IDs."
            )
        return ModelDiscoveryResult(
            models=tuple(sorted(model_ids)), request_id=_safe_request_id(response, api_key)
        )
    finally:
        if owns_client:
            await active_client.aclose()


def select_remote_model(
    requested_model: str | None,
    discovered_models: Sequence[str] | None,
) -> str:
    """Resolve a model without silently guessing among multiple paid targets."""

    requested = requested_model.strip() if requested_model else None
    discovered = tuple(dict.fromkeys(discovered_models or ()))
    if requested:
        if not _MODEL_ID_RE.fullmatch(requested):
            raise ProviderPreflightError(
                "invalid_model_id",
                "The requested model ID must contain 1 to 256 printable characters.",
            )
        if discovered and requested not in discovered:
            raise ProviderPreflightError(
                "requested_model_not_found",
                f"Requested model {requested!r} was not returned by the Provider.",
            )
        return requested
    if len(discovered) == 1:
        return discovered[0]
    if not discovered:
        raise ProviderPreflightError(
            "model_required",
            "The Provider did not expose a usable model list; pass --model explicitly.",
        )
    preview = ", ".join(repr(model) for model in discovered[:20])
    suffix = f" (+{len(discovered) - 20} more)" if len(discovered) > 20 else ""
    raise ProviderPreflightError(
        "model_selection_required",
        f"The Provider returned multiple models; pass --model. Candidates: {preview}{suffix}",
    )


async def run_chat_canary(
    base_url: str,
    remote_model_name: str,
    api_key_env: str,
    generation_config: Mapping[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> CanaryResult:
    """Make one minimal billed request using the same request fields as a Run."""

    _validated_base_url(base_url)
    adapter = OpenAICompatibleAdapter(
        base_url,
        remote_model_name,
        api_key_env,
        client=client,
    )
    config = {
        key: generation_config[key]
        for key in ("temperature", "top_p", "seed")
        if generation_config.get(key) is not None
    }
    config["max_tokens"] = min(int(generation_config.get("max_tokens", 16)), 16)
    try:
        result = await adapter.generate(
            [{"role": "user", "content": "Compatibility check: reply with exactly A."}],
            config,
        )
    except AdapterError as exc:
        raise ProviderPreflightError(
            f"canary_{exc.error_type}",
            f"Chat Completions canary failed: {exc.error_message}",
            status_code=exc.status_code,
        ) from exc
    finally:
        await adapter.aclose()

    returned_model = (
        str(result.metadata["returned_model"])
        if result.metadata.get("returned_model") is not None
        else None
    )
    if returned_model is not None and returned_model != remote_model_name:
        raise ProviderPreflightError(
            "canary_returned_model_mismatch",
            "The Provider canary returned a different model than the requested target.",
        )

    evaluated = get_evaluator("multiple_choice").evaluate(
        result.text,
        "A",
        {"choices": {"A": "expected", "B": "unexpected"}},
    )
    if evaluated.parsed_answer != "A":
        raise ProviderPreflightError(
            "canary_answer_unparseable",
            "The Provider responded, but its canary answer could not be parsed as option A.",
        )
    attempts = result.metadata.get("attempts", 1)
    return CanaryResult(
        model=remote_model_name,
        returned_model=returned_model,
        system_fingerprint=(
            str(result.metadata["system_fingerprint"])
            if result.metadata.get("system_fingerprint") is not None
            else None
        ),
        finish_reason=(
            str(result.metadata["finish_reason"])
            if result.metadata.get("finish_reason") is not None
            else None
        ),
        provider_request_id=result.provider_request_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=float(result.latency_ms),
        attempts=int(attempts) if isinstance(attempts, int) else 1,
    )
