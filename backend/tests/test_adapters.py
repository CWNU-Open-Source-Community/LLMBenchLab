from __future__ import annotations

import json

import httpx
import pytest

import app.adapters.openai_compatible as openai_compatible_module
from app.adapters import (
    AdapterError,
    MockModelAdapter,
    OpenAICompatibleAdapter,
    build_adapter,
)


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


@pytest.mark.asyncio
async def test_mock_adapter_is_predictable_and_offline() -> None:
    result = await MockModelAdapter().generate(
        [{"role": "user", "content": "ignored"}],
        {
            "mock_response": "A",
            "mock_input_tokens": 4,
            "mock_output_tokens": 1,
            "mock_latency_ms": 12.5,
            "mock_request_id": "mock-1",
        },
    )

    assert result.text == "A"
    assert result.input_tokens == 4
    assert result.output_tokens == 1
    assert result.latency_ms == 12.5
    assert result.provider_request_id == "mock-1"
    assert result.metadata == {"adapter": "mock", "offline": True}


@pytest.mark.asyncio
async def test_mock_adapter_supports_configured_errors() -> None:
    with pytest.raises(AdapterError) as caught:
        await MockModelAdapter().generate(
            [],
            {"mock_error": {"type": "rate_limited", "message": "try later", "retryable": True}},
        )

    assert caught.value.error_type == "rate_limited"
    assert caught.value.error_message == "try later"
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_mock_adapter_supports_bounded_offline_fault_delay() -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    result = await MockModelAdapter(sleep=fake_sleep).generate(
        [],
        {"mock_response": "A", "mock_generation_delay_seconds": 0.25},
    )
    assert result.text == "A"
    assert delays == [0.25]

    with pytest.raises(AdapterError, match="between 0 and 5 seconds"):
        await MockModelAdapter(sleep=fake_sleep).generate(
            [],
            {"mock_generation_delay_seconds": 5.1},
        )


@pytest.mark.asyncio
async def test_openai_compatible_sends_chat_completion_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "top-secret-key")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["accept_encoding"] = request.headers.get("accept-encoding")
        seen["payload"] = json.loads(request.content)
        seen["timeouts"] = request.extensions.get("timeout")
        return httpx.Response(
            200,
            json={
                "id": "provider-123",
                "model": "resolved-model-version",
                "system_fingerprint": "fp_123",
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "remote-model",
            "TEST_PROVIDER_KEY",
            client=client,
        )
        result = await adapter.generate(
            [{"role": "user", "content": "Hi"}],
            {
                "system_prompt": "Be concise.",
                "temperature": 0.2,
                "top_p": 0.9,
                "max_tokens": 50,
                "seed": 17,
                "not_forwarded": "secret-ish",
            },
        )

    assert seen["url"] == "https://provider.example/v1/chat/completions"
    assert seen["authorization"] == "Bearer top-secret-key"
    assert seen["accept_encoding"] == "identity"
    assert seen["payload"] == {
        "model": "remote-model",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hi"},
        ],
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": 50,
        "seed": 17,
    }
    assert seen["timeouts"] == {"connect": 5.0, "read": 60.0, "write": 30.0, "pool": 5.0}
    assert result.text == "hello"
    assert result.input_tokens == 7
    assert result.output_tokens == 2
    assert result.provider_request_id == "provider-123"
    assert result.metadata["attempts"] == 1
    assert result.metadata["returned_model"] == "resolved-model-version"
    assert result.metadata["system_fingerprint"] == "fp_123"
    assert result.metadata["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_openai_compatible_omits_null_max_tokens_for_provider_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "A"}, "finish_reason": "stop"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAICompatibleAdapter(
            "https://provider.example/v1", "model", "TEST_PROVIDER_KEY", client=client
        ).generate(
            [{"role": "user", "content": "question"}],
            {"temperature": 0, "max_tokens": None},
        )

    assert result.text == "A"
    assert seen_payload["temperature"] == 0
    assert "max_tokens" not in seen_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish_reason", "expected_error"),
    [("length", "output_truncated"), ("stop", "empty_response")],
)
async def test_openai_compatible_classifies_empty_output_budget_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    finish_reason: str,
    expected_error: str,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "   "}, "finish_reason": finish_reason}]},
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1", "model", "TEST_PROVIDER_KEY", client=client
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "question"}], {})

    assert caught.value.error_type == expected_error
    if finish_reason == "length":
        assert "Increase max_tokens" in caught.value.error_message


@pytest.mark.asyncio
async def test_openai_compatible_classifies_missing_content_after_length_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"choices": [{"message": {}, "finish_reason": "length"}]},
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1", "model", "TEST_PROVIDER_KEY", client=client
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "question"}], {})

    assert caught.value.error_type == "output_truncated"
    assert "Increase max_tokens" in caught.value.error_message


@pytest.mark.asyncio
async def test_openai_compatible_allows_missing_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "A"}}]})
    )

    async with httpx.AsyncClient(transport=transport) as client:
        result = await OpenAICompatibleAdapter(
            "https://provider.example/v1", "model", "TEST_PROVIDER_KEY", client=client
        ).generate([{"role": "system", "content": "System stays intact"}], {})

    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.raw_usage is None


@pytest.mark.asyncio
async def test_openai_compatible_redacts_exact_key_from_success_content_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-test.+/=_~[]{}()"
    monkeypatch.setenv("TEST_PROVIDER_KEY", secret)
    echoed = f"plain={secret}\nwrapped=<{secret}>\njson-ish={{'key':'{secret}'}}"
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "id": f"request-{secret}",
                "model": f"model-{secret}",
                "system_fingerprint": f"fingerprint-{secret}",
                "choices": [{"message": {"content": echoed}, "finish_reason": f"stop-{secret}"}],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 2,
                    f"custom-{secret}": f"value:{secret}",
                    "nested": [secret, {f"key-{secret}": secret}],
                },
            },
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        result = await OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            client=client,
        ).generate([{"role": "user", "content": "hello"}], {})

    assert result.text == ("plain=[REDACTED]\nwrapped=<[REDACTED]>\njson-ish={'key':'[REDACTED]'}")
    assert result.input_tokens == 7
    assert result.output_tokens == 2
    assert result.raw_usage == {
        "prompt_tokens": 7,
        "completion_tokens": 2,
        "custom-[REDACTED]": "value:[REDACTED]",
        "nested": ["[REDACTED]", {"key-[REDACTED]": "[REDACTED]"}],
    }
    assert result.metadata["finish_reason"] == "stop-[REDACTED]"
    assert secret not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "limit_name"),
    [
        (200, "MAX_CHAT_SUCCESS_RESPONSE_BYTES"),
        (503, "MAX_CHAT_ERROR_RESPONSE_BYTES"),
    ],
)
async def test_openai_compatible_streams_and_rejects_oversized_response_bodies(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    limit_name: str,
) -> None:
    secret = "oversized-response-secret"
    monkeypatch.setenv("TEST_PROVIDER_KEY", secret)
    monkeypatch.setattr(openai_compatible_module, limit_name, 32)
    monkeypatch.setattr(openai_compatible_module, "_RESPONSE_READ_CHUNK_BYTES", 8)
    stream = _TrackedAsyncByteStream([secret.encode()] * 10)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            headers={"content-type": "application/json"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            max_retries=2,
            retry_backoff_base_seconds=0,
            client=client,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "hello"}], {})

    assert calls == 1
    assert caught.value.error_type == "provider_response_too_large"
    assert caught.value.status_code == status_code
    assert caught.value.attempts == 1
    assert caught.value.retryable is False
    assert "32-byte safety limit" in caught.value.error_message
    assert secret not in str(caught.value)
    assert stream.yielded < len(stream.chunks)
    assert stream.closed is True


@pytest.mark.asyncio
async def test_openai_compatible_rejects_compressed_response_without_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    stream = _TrackedAsyncByteStream([b"compressed-body-must-not-be-read"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            client=client,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "hello"}], {})

    assert caught.value.error_type == "unsupported_provider_response_encoding"
    assert caught.value.status_code == 200
    assert stream.yielded == 0
    assert stream.closed is True


@pytest.mark.asyncio
async def test_openai_compatible_reuses_and_closes_owned_client_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "A"}}]})

    adapter = OpenAICompatibleAdapter("https://provider.example/v1", "model", "TEST_PROVIDER_KEY")
    owned_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter._client = owned_client  # type: ignore[attr-defined]  # Exercise owned-pool lifecycle.

    await adapter.generate([{"role": "user", "content": "one"}], {})
    await adapter.generate([{"role": "user", "content": "two"}], {})
    assert requests == 2
    assert owned_client.is_closed is False

    await adapter.aclose()
    assert owned_client.is_closed is True
    assert adapter._client is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_openai_compatible_retries_429_and_selected_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    statuses = iter([429, 503, 200])
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status == 200:
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        return httpx.Response(status, json={"error": {"message": "temporarily unavailable"}})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            max_retries=2,
            retry_backoff_base_seconds=0.1,
            client=client,
            sleep=fake_sleep,
        ).generate([{"role": "user", "content": "hello"}], {})

    assert result.text == "ok"
    assert result.metadata["attempts"] == 3
    assert delays == [0.1, 0.2]


@pytest.mark.asyncio
async def test_openai_compatible_does_not_retry_plain_4xx_and_redacts_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "super-secret-key"
    monkeypatch.setenv("TEST_PROVIDER_KEY", secret)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400,
            json={"error": {"message": f"Authorization: Bearer {secret}; api_key={secret}"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1", "model", "TEST_PROVIDER_KEY", client=client
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "hello"}], {})

    assert calls == 1
    assert caught.value.error_type == "provider_4xx"
    assert caught.value.retryable is False
    assert secret not in str(caught.value)
    assert "Authorization: Bearer" not in str(caught.value)


@pytest.mark.asyncio
async def test_openai_compatible_retries_and_classifies_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "network-secret"
    monkeypatch.setenv("TEST_PROVIDER_KEY", secret)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(f"Bearer {secret}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            max_retries=1,
            retry_backoff_base_seconds=0,
            client=client,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "hello"}], {})

    assert calls == 2
    assert caught.value.error_type == "network_error"
    assert caught.value.attempts == 2
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_openai_compatible_requires_key_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)
    adapter = OpenAICompatibleAdapter(
        "https://provider.example/v1", "model", "MISSING_PROVIDER_KEY"
    )

    with pytest.raises(AdapterError) as caught:
        await adapter.generate([{"role": "user", "content": "hello"}], {})

    assert caught.value.error_type == "missing_api_key"


@pytest.mark.asyncio
async def test_openai_compatible_rejects_empty_provider_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": " "}}]})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1", "model", "TEST_PROVIDER_KEY", client=client
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "hello"}], {})

    assert caught.value.error_type == "empty_response"


def test_adapter_registry() -> None:
    assert isinstance(build_adapter("mock", base_url="ignored"), MockModelAdapter)
    assert isinstance(
        build_adapter(
            "openai_compatible",
            base_url="https://provider.example/v1",
            remote_model_name="model",
            api_key_env="KEY",
            connect_timeout=None,
        ),
        OpenAICompatibleAdapter,
    )
    with pytest.raises(ValueError, match="Unsupported provider_type"):
        build_adapter("unknown")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://provider.example/v1",
        "http://localhost:11434/v1",
        "http://localhost.:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://127.255.255.254:11434/v1",
        "http://[::1]:11434/v1",
    ],
)
def test_openai_compatible_allows_https_or_loopback_http(base_url: str) -> None:
    adapter = OpenAICompatibleAdapter(base_url, "model", "KEY")
    assert adapter.base_url == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "http://provider.example/v1",
        "http://localhost.example/v1",
        "http://10.0.0.1/v1",
        "http://192.168.1.1/v1",
        "http://[::2]/v1",
    ],
)
def test_openai_compatible_rejects_non_loopback_plain_http(base_url: str) -> None:
    with pytest.raises(ValueError, match="only for loopback hosts"):
        OpenAICompatibleAdapter(base_url, "model", "KEY")


@pytest.mark.parametrize(
    ("base_url", "message"),
    [
        ("provider.example/v1", "absolute HTTP"),
        ("ftp://provider.example/v1", "absolute HTTP"),
        ("https://user:pass@provider.example/v1", "embedded credentials"),
        ("https://provider.example/v1?token=value", "query parameters"),
        ("https://provider.example/v1#fragment", "URL fragment"),
    ],
)
def test_openai_compatible_direct_construction_rejects_unsafe_urls(
    base_url: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        OpenAICompatibleAdapter(base_url, "model", "KEY")


@pytest.mark.parametrize("invalid_retries", [True, 1.5, -1])
def test_openai_compatible_validates_retry_count(invalid_retries: object) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "KEY",
            max_retries=invalid_retries,  # type: ignore[arg-type]
        )
