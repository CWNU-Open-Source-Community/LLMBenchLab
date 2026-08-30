from __future__ import annotations

import json
import traceback

import httpx
import pytest

from app.adapters import (
    AdapterError,
    OpenAICompatibleAdapter,
    ProviderAttemptContext,
    ProviderAttemptDisposition,
    ProviderAttemptOutcome,
    ProviderAttemptPermit,
)
from app.adapters.provider_protocols import (
    AnthropicMessagesAdapter,
    OpenAIResponsesAdapter,
)


def _typed_sse(event_type: str, body: dict[str, object]) -> bytes:
    return (
        f"event: {event_type}\n".encode()
        + b"data: "
        + json.dumps(body, ensure_ascii=False).encode()
        + b"\n\n"
    )


class _AttemptController:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    async def reserve(
        self,
        context: ProviderAttemptContext,
        *,
        provider_attempt: int,
    ) -> ProviderAttemptPermit:
        self.events.append(("reserve", provider_attempt, context.question_id))
        return ProviderAttemptPermit(
            reservation_id=f"reservation-{provider_attempt}",
            provider_attempt=provider_attempt,
        )

    async def mark_send_started(self, permit: ProviderAttemptPermit) -> None:
        self.events.append(("mark", permit.provider_attempt))

    async def finish(
        self,
        permit: ProviderAttemptPermit,
        *,
        disposition: ProviderAttemptDisposition,
        outcome: ProviderAttemptOutcome,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self.events.append(
            (
                "finish",
                permit.provider_attempt,
                disposition,
                outcome,
                input_tokens,
                output_tokens,
            )
        )


def _attempt_context() -> ProviderAttemptContext:
    return ProviderAttemptContext(
        run_id="run-1",
        question_id="question-1",
        model_id="model-1",
        provider_scope="provider-1",
        lease_token=3,
        execution_generation=2,
        next_provider_attempt=1,
        reserved_input_tokens=16,
        reserved_output_tokens=8,
    )


@pytest.mark.parametrize(
    ("adapter_type", "root_url", "full_url"),
    [
        (
            OpenAIResponsesAdapter,
            "https://provider.example/v1",
            "https://provider.example/v1/responses",
        ),
        (
            AnthropicMessagesAdapter,
            "https://provider.example/v1",
            "https://provider.example/v1/messages",
        ),
    ],
)
def test_explicit_protocol_adapters_accept_root_or_matching_full_endpoint(
    adapter_type: type[OpenAICompatibleAdapter],
    root_url: str,
    full_url: str,
) -> None:
    assert adapter_type(root_url, "model", api_key="key").endpoint_url == full_url
    assert adapter_type(full_url, "model", api_key="key").endpoint_url == full_url


@pytest.mark.parametrize(
    ("adapter_type", "wrong_url"),
    [
        (OpenAICompatibleAdapter, "https://provider.example/v1/responses"),
        (OpenAICompatibleAdapter, "https://provider.example/v1/messages"),
        (OpenAIResponsesAdapter, "https://provider.example/v1/chat/completions"),
        (OpenAIResponsesAdapter, "https://provider.example/v1/messages"),
        (AnthropicMessagesAdapter, "https://provider.example/v1/chat/completions"),
        (AnthropicMessagesAdapter, "https://provider.example/v1/responses"),
    ],
)
def test_explicit_protocol_adapters_reject_known_wrong_endpoint_suffix(
    adapter_type: type[OpenAICompatibleAdapter],
    wrong_url: str,
) -> None:
    with pytest.raises(ValueError, match="does not match the selected"):
        adapter_type(wrong_url, "model", api_key="key")


@pytest.mark.parametrize("adapter_type", [OpenAIResponsesAdapter, AnthropicMessagesAdapter])
@pytest.mark.asyncio
async def test_protocol_json_parse_errors_do_not_retain_provider_secrets(
    adapter_type: type[OpenAICompatibleAdapter],
) -> None:
    secret = "malformed-json-provider-secret"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=f'{{"reflected":"{secret}"'.encode(),
            request=request,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AdapterError) as caught:
            await adapter_type(
                "https://provider.example/v1",
                "model",
                api_key=secret,
                client=client,
            ).generate(
                [{"role": "user", "content": "question"}],
                {"max_tokens": 16},
            )

    assert caught.value.error_type == "invalid_provider_response"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    formatted = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert secret not in formatted


@pytest.mark.parametrize(
    ("adapter_type", "event_type"),
    [
        (OpenAIResponsesAdapter, "response.output_text.delta"),
        (AnthropicMessagesAdapter, "content_block_delta"),
    ],
)
@pytest.mark.asyncio
async def test_protocol_sse_parse_errors_do_not_retain_provider_secrets(
    adapter_type: type[OpenAICompatibleAdapter],
    event_type: str,
) -> None:
    secret = "malformed-sse-provider-secret"
    payload = f'event: {event_type}\ndata: {{"reflected":"{secret}"\n\n'.encode()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=payload,
            request=request,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AdapterError) as caught:
            await adapter_type(
                "https://provider.example/v1",
                "model",
                api_key=secret,
                client=client,
            ).generate(
                [{"role": "user", "content": "question"}],
                {"max_tokens": 16},
            )

    assert caught.value.error_type == "invalid_provider_stream"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    formatted = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert secret not in formatted


@pytest.mark.asyncio
async def test_responses_json_request_and_result_contract() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["accept_encoding"] = request.headers.get("accept-encoding")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp-1",
                "model": "resolved-model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "first "},
                            {"type": "refusal", "refusal": "ignored"},
                            {"type": "output_text", "text": "answer"},
                        ],
                    }
                ],
                "usage": {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAIResponsesAdapter(
            "https://provider.example/v1",
            "remote-model",
            api_key="responses-key",
            client=client,
        ).generate(
            [{"role": "user", "content": "question"}],
            {
                "system_prompt": "Be concise.",
                "temperature": 0.2,
                "top_p": 0.9,
                "max_tokens": 64,
            },
        )

    assert seen == {
        "url": "https://provider.example/v1/responses",
        "authorization": "Bearer responses-key",
        "accept_encoding": "identity",
        "payload": {
            "model": "remote-model",
            "input": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "question"},
            ],
            "stream": True,
            "temperature": 0.2,
            "top_p": 0.9,
            "max_output_tokens": 64,
        },
    }
    assert result.text == "first answer"
    assert result.input_tokens == 11
    assert result.output_tokens == 3
    assert result.provider_request_id == "resp-1"
    assert result.metadata == {
        "adapter": "openai_responses",
        "attempts": 1,
        "response_mode": "json",
        "finish_reason": "completed",
        "returned_model": "resolved-model",
    }


@pytest.mark.asyncio
async def test_responses_typed_sse_requires_completed_and_uses_terminal_usage() -> None:
    stream = b"".join(
        [
            _typed_sse(
                "response.created",
                {
                    "type": "response.created",
                    "response": {"id": "resp-sse", "model": "model", "status": "in_progress"},
                },
            ),
            _typed_sse(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "stream "},
            ),
            _typed_sse(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "answer"},
            ),
            _typed_sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-sse",
                        "model": "model",
                        "status": "completed",
                        "output": [],
                        "usage": {"input_tokens": 9, "output_tokens": 2},
                    },
                },
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stream,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAIResponsesAdapter(
            "https://provider.example/v1",
            "model",
            api_key="key",
            client=client,
        ).generate([{"role": "user", "content": "question"}], {})

    assert result.text == "stream answer"
    assert result.input_tokens == 9
    assert result.output_tokens == 2
    assert result.metadata["adapter"] == "openai_responses"
    assert result.metadata["response_mode"] == "sse"


@pytest.mark.asyncio
async def test_responses_stream_eof_without_completed_fails_closed() -> None:
    stream = _typed_sse(
        "response.output_text.delta",
        {"type": "response.output_text.delta", "delta": "partial"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stream,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAIResponsesAdapter(
            "https://provider.example/v1",
            "model",
            api_key="key",
            client=client,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "question"}], {})

    assert caught.value.error_type == "incomplete_provider_stream"
    assert "response.completed" in caught.value.error_message


@pytest.mark.asyncio
async def test_responses_rejects_seed_before_sending_or_retrying() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAIResponsesAdapter(
            "https://provider.example/v1",
            "model",
            api_key="key",
            client=client,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate(
                [{"role": "user", "content": "question"}],
                {"seed": 0},
            )

    assert caught.value.error_type == "invalid_request"
    assert calls == 0


@pytest.mark.asyncio
async def test_responses_reuses_retry_and_attempt_settlement_contract() -> None:
    calls = 0
    delays: list[float] = []
    controller = _AttemptController()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "retry"}}, request=request)
        return httpx.Response(
            200,
            json={
                "id": "resp-retried",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "answer"}],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
            request=request,
        )

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAIResponsesAdapter(
            "https://provider.example/v1",
            "model",
            api_key="key",
            client=client,
            max_retries=1,
            sleep=fake_sleep,
            attempt_controller=controller,
        ).generate(
            [{"role": "user", "content": "question"}],
            {},
            attempt_context=_attempt_context(),
        )

    assert result.text == "answer"
    assert result.metadata["attempts"] == 2
    assert delays == [0.25]
    assert controller.events == [
        ("reserve", 1, "question-1"),
        ("mark", 1),
        (
            "finish",
            1,
            ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
            ProviderAttemptOutcome.HTTP_ERROR,
            None,
            None,
        ),
        ("reserve", 2, "question-1"),
        ("mark", 2),
        (
            "finish",
            2,
            ProviderAttemptDisposition.SETTLED_ACTUAL,
            ProviderAttemptOutcome.SUCCEEDED,
            3,
            1,
        ),
    ]


@pytest.mark.asyncio
async def test_responses_reuses_body_limit_and_does_not_follow_redirects() -> None:
    redirect_calls = 0

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        nonlocal redirect_calls
        redirect_calls += 1
        return httpx.Response(
            307,
            headers={"location": "https://other.example/v1/responses"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect_handler)) as client:
        adapter = OpenAIResponsesAdapter(
            "https://provider.example/v1",
            "model",
            api_key="key",
            client=client,
        )
        with pytest.raises(AdapterError) as redirect_error:
            await adapter.generate([{"role": "user", "content": "question"}], {})

    assert redirect_error.value.error_type == "provider_http_error"
    assert redirect_calls == 1

    def oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(4 * 1024 * 1024 + 1)},
            content=b"{}",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(oversized_handler)) as client:
        adapter = OpenAIResponsesAdapter(
            "https://provider.example/v1",
            "model",
            api_key="key",
            client=client,
        )
        with pytest.raises(AdapterError) as oversized_error:
            await adapter.generate([{"role": "user", "content": "question"}], {})

    assert oversized_error.value.error_type == "provider_response_too_large"


@pytest.mark.asyncio
async def test_messages_json_request_and_result_contract() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["x_api_key"] = request.headers.get("x-api-key")
        seen["authorization"] = request.headers.get("authorization")
        seen["anthropic_version"] = request.headers.get("anthropic-version")
        seen["accept_encoding"] = request.headers.get("accept-encoding")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg-1",
                "type": "message",
                "model": "resolved-model",
                "content": [
                    {"type": "text", "text": "message "},
                    {"type": "thinking", "thinking": "ignored"},
                    {"type": "text", "text": "answer"},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 13, "output_tokens": 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AnthropicMessagesAdapter(
            "https://provider.example/v1/messages",
            "remote-model",
            api_key="messages-key",
            client=client,
        ).generate(
            [{"role": "user", "content": "question"}],
            {
                "system_prompt": "Be concise.",
                "temperature": 0.1,
                "top_p": 0.8,
                "max_tokens": 72,
            },
        )

    assert seen == {
        "url": "https://provider.example/v1/messages",
        "x_api_key": "messages-key",
        "authorization": None,
        "anthropic_version": "2023-06-01",
        "accept_encoding": "identity",
        "payload": {
            "model": "remote-model",
            "messages": [{"role": "user", "content": "question"}],
            "stream": True,
            "max_tokens": 72,
            "system": "Be concise.",
            "temperature": 0.1,
            "top_p": 0.8,
        },
    }
    assert result.text == "message answer"
    assert result.input_tokens == 13
    assert result.output_tokens == 4
    assert result.provider_request_id == "msg-1"
    assert result.metadata == {
        "adapter": "anthropic_messages",
        "attempts": 1,
        "response_mode": "json",
        "finish_reason": "end_turn",
        "returned_model": "resolved-model",
    }


@pytest.mark.asyncio
async def test_messages_rejects_temperature_above_protocol_limit_before_sending() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = AnthropicMessagesAdapter(
            "https://provider.example/v1",
            "messages-model",
            api_key="fake-key",
            client=client,
        )
        with pytest.raises(AdapterError, match="temperature") as caught:
            await adapter.generate(
                [{"role": "user", "content": "question"}],
                {"max_tokens": 16, "temperature": 1.5},
            )

    assert caught.value.error_type == "invalid_request"
    assert calls == 0


@pytest.mark.asyncio
async def test_messages_empty_max_tokens_response_is_output_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg-truncated",
                "type": "message",
                "model": "model",
                "content": [],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 4, "output_tokens": 8},
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = AnthropicMessagesAdapter(
            "https://provider.example/v1",
            "model",
            api_key="key",
            client=client,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate(
                [{"role": "user", "content": "question"}],
                {"max_tokens": 8},
            )

    assert caught.value.error_type == "output_truncated"


@pytest.mark.asyncio
async def test_messages_typed_sse_merges_cumulative_usage_without_summing() -> None:
    stream = b"".join(
        [
            _typed_sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-sse",
                        "model": "model",
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": 15, "output_tokens": 1},
                    },
                },
            ),
            _typed_sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "stream answer"},
                },
            ),
            _typed_sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 5},
                },
            ),
            _typed_sse("message_stop", {"type": "message_stop"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stream,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AnthropicMessagesAdapter(
            "https://provider.example/v1",
            "model",
            api_key="key",
            client=client,
        ).generate(
            [{"role": "user", "content": "question"}],
            {"max_tokens": 64},
        )

    assert result.text == "stream answer"
    assert result.input_tokens == 15
    assert result.output_tokens == 5
    assert result.raw_usage == {"input_tokens": 15, "output_tokens": 5}
    assert result.metadata["adapter"] == "anthropic_messages"
    assert result.metadata["response_mode"] == "sse"


@pytest.mark.asyncio
async def test_messages_stream_eof_without_message_stop_fails_closed() -> None:
    stream = _typed_sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg-sse",
                "model": "model",
                "content": [],
                "usage": {"input_tokens": 2, "output_tokens": 0},
            },
        },
    ) + _typed_sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "partial"},
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stream,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = AnthropicMessagesAdapter(
            "https://provider.example/v1",
            "model",
            api_key="key",
            client=client,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate(
                [{"role": "user", "content": "question"}],
                {"max_tokens": 64},
            )

    assert caught.value.error_type == "incomplete_provider_stream"
    assert "message_stop" in caught.value.error_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "generation_config",
    [
        {"max_tokens": None},
        {"max_tokens": 64, "seed": 1},
    ],
)
async def test_messages_rejects_invalid_parameters_before_sending(
    generation_config: dict[str, object],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = AnthropicMessagesAdapter(
            "https://provider.example/v1",
            "model",
            api_key="key",
            client=client,
        )
        with pytest.raises(AdapterError):
            await adapter.generate(
                [{"role": "user", "content": "question"}],
                generation_config,
            )

    assert calls == 0


@pytest.mark.asyncio
async def test_new_protocols_redact_current_key_from_results_and_http_errors() -> None:
    secret = "current-provider-key"

    def responses_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": f"id-{secret}",
                "model": f"model-{secret}",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": f"answer {secret}"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "echo": secret},
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responses_handler)) as client:
        result = await OpenAIResponsesAdapter(
            "https://provider.example/v1",
            "model",
            api_key=secret,
            client=client,
        ).generate([{"role": "user", "content": "question"}], {})

    rendered = repr(result)
    assert secret not in rendered
    assert "[REDACTED]" in rendered

    def messages_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": f"bad key {secret}"}},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(messages_handler)) as client:
        adapter = AnthropicMessagesAdapter(
            "https://provider.example/v1",
            "model",
            api_key=secret,
            client=client,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate(
                [{"role": "user", "content": "question"}],
                {"max_tokens": 16},
            )

    assert secret not in caught.value.error_message
    assert "[REDACTED]" in caught.value.error_message
