from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from app.models import ProviderType
from app.providers import (
    ProviderPreflightError,
    discover_models,
    models_url,
    run_chat_canary,
    select_remote_model,
)
from app.schemas.model import ModelCreate


class _TrackedAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://provider.example/v1", "https://provider.example/v1/models"),
        (
            "https://provider.example/openai/v1/chat/completions",
            "https://provider.example/openai/v1/models",
        ),
        ("http://127.0.0.1:11434/v1/", "http://127.0.0.1:11434/v1/models"),
        ("http://localhost:11434/v1", "http://localhost:11434/v1/models"),
        ("http://[::1]:11434/v1", "http://[::1]:11434/v1/models"),
    ],
)
def test_models_url_uses_compatible_endpoint_root(base_url: str, expected: str) -> None:
    assert models_url(base_url) == expected


@pytest.mark.parametrize(
    "invalid",
    [
        "provider.example/v1",
        "ftp://provider.example/v1",
        "https://user:pass@provider.example/v1",
        "https://provider.example/v1?token=secret",
        "https://provider.example/v1#fragment",
        "http://provider.example/v1",
        "http://localhost.example/v1",
        "http://192.168.1.10/v1",
        "http://0.0.0.0/v1",
    ],
)
def test_models_url_rejects_unsafe_or_ambiguous_urls(invalid: str) -> None:
    with pytest.raises(ProviderPreflightError) as caught:
        models_url(invalid)
    assert caught.value.code == "invalid_base_url"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://provider.example/v1",
        "http://localhost:11434/v1",
        "http://localhost.:11434/v1",
        "http://127.0.0.2:11434/v1",
        "http://[::1]:11434/v1",
    ],
)
def test_model_schema_allows_https_or_loopback_http(base_url: str) -> None:
    model = ModelCreate(
        name="Provider",
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        base_url=base_url,
        remote_model_name="remote-model",
        api_key_env="PROVIDER_KEY",
    )

    assert model.base_url == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "http://provider.example/v1",
        "http://localhost.example/v1",
        "http://10.0.0.2/v1",
        "http://0.0.0.0/v1",
    ],
)
def test_model_schema_rejects_non_loopback_plain_http(base_url: str) -> None:
    with pytest.raises(ValidationError, match="only for loopback hosts"):
        ModelCreate(
            name="Provider",
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            base_url=base_url,
            remote_model_name="remote-model",
            api_key_env="PROVIDER_KEY",
        )


@pytest.mark.asyncio
async def test_discover_models_authenticates_and_returns_sorted_unique_ids() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["accept_encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(
            200,
            headers={"x-request-id": "discovery-1"},
            json={
                "object": "list",
                "data": [
                    {"id": "z-model"},
                    {"id": "a-model"},
                    {"id": "z-model"},
                    {"not_id": "ignored"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_models("https://provider.example/v1", "top-secret", client=client)

    assert seen == {
        "url": "https://provider.example/v1/models",
        "authorization": "Bearer top-secret",
        "accept_encoding": "identity",
    }
    assert result.models == ("a-model", "z-model")
    assert result.request_id == "discovery-1"


@pytest.mark.asyncio
async def test_discover_models_stops_reading_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.providers.preflight.MAX_DISCOVERY_RESPONSE_BYTES", 16)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b'{"data":[' + b" " * 32 + b"]}")
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderPreflightError) as caught:
            await discover_models("https://provider.example/v1", "secret", client=client)

    assert caught.value.code == "model_discovery_response_too_large"


@pytest.mark.asyncio
async def test_discover_models_rejects_compressed_response_without_decoding() -> None:
    stream = _TrackedAsyncByteStream([b"compressed-body-must-not-be-read"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderPreflightError) as caught:
            await discover_models("https://provider.example/v1", "secret", client=client)

    assert caught.value.code == "unsupported_model_discovery_response_encoding"
    assert caught.value.status_code == 200
    assert stream.yielded == 0
    assert stream.closed is True


@pytest.mark.asyncio
async def test_discover_models_classifies_auth_failure_and_redacts_key() -> None:
    secret = "discovery-super-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": f"Authorization: Bearer {secret}; api_key={secret}"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderPreflightError) as caught:
            await discover_models("https://provider.example/v1", secret, client=client)

    assert caught.value.code == "model_discovery_authentication_error"
    assert caught.value.status_code == 401
    assert secret not in caught.value.message
    assert "Bearer" not in caught.value.message


@pytest.mark.parametrize(
    "reflected_model_id",
    [
        "fixture-discovery-secret",
        "model-fixture-discovery-secret-suffix",
    ],
)
@pytest.mark.asyncio
async def test_discover_models_rejects_api_key_reflection_without_echoing_it(
    reflected_model_id: str,
) -> None:
    secret = "fixture-discovery-secret"
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": [{"id": "safe-model"}, {"id": reflected_model_id}]},
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderPreflightError) as caught:
            await discover_models("https://provider.example/v1", secret, client=client)

    assert caught.value.code == "model_discovery_secret_reflection"
    assert secret not in caught.value.message
    assert secret not in str(caught.value)


def test_select_remote_model_never_guesses_between_multiple_targets() -> None:
    assert select_remote_model(None, ["only-model"]) == "only-model"
    assert select_remote_model("chosen", None) == "chosen"
    assert select_remote_model("chosen", ["chosen", "other"]) == "chosen"

    with pytest.raises(ProviderPreflightError) as caught:
        select_remote_model(None, ["one", "two"])
    assert caught.value.code == "model_selection_required"
    assert "one" in caught.value.message
    assert "two" in caught.value.message

    with pytest.raises(ProviderPreflightError) as missing:
        select_remote_model("missing", ["present"])
    assert missing.value.code == "requested_model_not_found"

    with pytest.raises(ProviderPreflightError) as invalid:
        select_remote_model("bad\nmodel", None)
    assert invalid.value.code == "invalid_model_id"


@pytest.mark.asyncio
async def test_chat_canary_uses_run_fields_and_requires_parseable_a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CANARY_KEY", "secret")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["accept"] = request.headers.get("accept")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"x-request-id": "header-request"},
            json={
                "id": "canary-request",
                "model": "remote-model",
                "choices": [{"message": {"content": "The answer is A"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_chat_canary(
            "https://provider.example/v1",
            "remote-model",
            "CANARY_KEY",
            {
                "temperature": 0,
                "top_p": 1,
                "max_tokens": 4096,
                "seed": 42,
            },
            client=client,
        )

    assert seen["url"] == "https://provider.example/v1/chat/completions"
    assert seen["accept"] == "text/event-stream"
    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "remote-model"
    assert payload["max_tokens"] == 16
    assert payload["temperature"] == 0
    assert payload["top_p"] == 1
    assert payload["seed"] == 42
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["messages"] == [
        {"role": "user", "content": "Compatibility check: reply with exactly A."}
    ]
    assert result.provider_request_id == "canary-request"
    assert result.input_tokens == 9
    assert result.output_tokens == 4
    assert result.attempts == 1


@pytest.mark.asyncio
async def test_chat_canary_redacts_key_reflected_in_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "canary-finish-reason-secret"
    monkeypatch.setenv("CANARY_KEY", secret)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "model": "remote-model",
                "choices": [
                    {
                        "message": {"content": "A"},
                        "finish_reason": f"stop-{secret}",
                    }
                ],
            },
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        result = await run_chat_canary(
            "https://provider.example/v1",
            "remote-model",
            "CANARY_KEY",
            {},
            client=client,
        )

    assert result.finish_reason == "stop-[REDACTED]"
    assert secret not in repr(result)


@pytest.mark.asyncio
async def test_chat_canary_rejects_unparseable_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CANARY_KEY", "secret")
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "I cannot choose."}}]},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderPreflightError) as caught:
            await run_chat_canary(
                "https://provider.example/v1",
                "model",
                "CANARY_KEY",
                {},
                client=client,
            )
    assert caught.value.code == "canary_answer_unparseable"


@pytest.mark.asyncio
async def test_chat_canary_rejects_a_different_returned_model_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CANARY_KEY", "secret")
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "model": "different-model",
                "choices": [{"message": {"content": "A"}}],
            },
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderPreflightError) as caught:
            await run_chat_canary(
                "https://provider.example/v1",
                "requested-model",
                "CANARY_KEY",
                {},
                client=client,
            )

    assert caught.value.code == "canary_returned_model_mismatch"
    assert "requested-model" not in caught.value.message
    assert "different-model" not in caught.value.message


@pytest.mark.asyncio
async def test_chat_canary_accepts_a_missing_returned_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CANARY_KEY", "secret")
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "A"}}]},
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        result = await run_chat_canary(
            "https://provider.example/v1",
            "requested-model",
            "CANARY_KEY",
            {},
            client=client,
        )

    assert result.model == "requested-model"
    assert result.returned_model is None


@pytest.mark.asyncio
async def test_chat_canary_rejects_remote_plain_http_before_sending_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CANARY_KEY", "secret")

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        pytest.fail("remote plaintext HTTP must be rejected before a request is sent")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        with pytest.raises(ProviderPreflightError) as caught:
            await run_chat_canary(
                "http://provider.example/v1",
                "requested-model",
                "CANARY_KEY",
                {},
                client=client,
            )

    assert caught.value.code == "invalid_base_url"
