from __future__ import annotations

import json

import httpx
import pytest

from app.adapters import (
    AdapterError,
    MockModelAdapter,
    OpenAICompatibleAdapter,
    build_adapter,
)


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
        seen["payload"] = json.loads(request.content)
        seen["timeouts"] = request.extensions.get("timeout")
        return httpx.Response(
            200,
            json={
                "id": "provider-123",
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


@pytest.mark.parametrize("invalid_retries", [True, 1.5, -1])
def test_openai_compatible_validates_retry_count(invalid_retries: object) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "KEY",
            max_retries=invalid_retries,  # type: ignore[arg-type]
        )
