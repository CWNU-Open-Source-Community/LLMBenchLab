"""Registry-level coverage for explicit remote Provider protocols."""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters import (
    AdapterError,
    AnthropicMessagesAdapter,
    OpenAIResponsesAdapter,
    ProviderAttemptContext,
    ProviderAttemptDisposition,
    ProviderAttemptOutcome,
    ProviderAttemptPermit,
    build_adapter,
)


def _typed_sse(event_type: str, body: dict[str, object]) -> bytes:
    return f"event: {event_type}\n".encode() + b"data: " + json.dumps(body).encode() + b"\n\n"


class _AttemptController:
    def __init__(self) -> None:
        self.finishes: list[tuple[object, ...]] = []

    async def reserve(
        self,
        context: ProviderAttemptContext,
        *,
        provider_attempt: int,
    ) -> ProviderAttemptPermit:
        del context
        return ProviderAttemptPermit(
            reservation_id=f"reservation-{provider_attempt}",
            provider_attempt=provider_attempt,
        )

    async def mark_send_started(self, permit: ProviderAttemptPermit) -> None:
        del permit

    async def finish(
        self,
        permit: ProviderAttemptPermit,
        *,
        disposition: ProviderAttemptDisposition,
        outcome: ProviderAttemptOutcome,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self.finishes.append(
            (
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
        provider_scope="provider-scope",
        lease_token=1,
        execution_generation=1,
        next_provider_attempt=1,
    )


@pytest.mark.parametrize(
    ("provider_type", "adapter_class", "expected_url"),
    [
        (
            "openai_responses",
            OpenAIResponsesAdapter,
            "https://provider.example/zen/go/v1/responses",
        ),
        (
            "anthropic_messages",
            AnthropicMessagesAdapter,
            "https://provider.example/zen/go/v1/messages",
        ),
    ],
)
def test_registry_builds_explicit_protocol_adapter(
    provider_type: str,
    adapter_class: type,
    expected_url: str,
) -> None:
    adapter = build_adapter(
        provider_type,
        base_url="https://provider.example/zen/go/v1",
        remote_model_name="remote-model",
        api_key_env="PROTOCOL_PROVIDER_KEY",
    )

    assert isinstance(adapter, adapter_class)
    assert adapter.endpoint_url == expected_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_kind",
    [
        "responses_rate_limit",
        "responses_server_error",
        "responses_json_rate_limit",
        "responses_json_server_error",
        "messages_overloaded",
        "messages_rate_limit",
        "messages_api_error",
        "messages_timeout_error",
        "messages_http_529",
    ],
)
async def test_typed_transient_failures_retry_and_settle_each_attempt(
    failure_kind: str,
) -> None:
    calls = 0
    controller = _AttemptController()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure_kind == "responses_json_rate_limit":
                return httpx.Response(
                    200,
                    json={
                        "status": "failed",
                        "error": {"code": "rate_limit_exceeded", "message": "retry later"},
                    },
                    request=request,
                )
            if failure_kind == "responses_json_server_error":
                return httpx.Response(
                    200,
                    json={
                        "status": "failed",
                        "error": {"code": "server_error", "message": "retry later"},
                    },
                    request=request,
                )
            if failure_kind == "responses_rate_limit":
                stream = _typed_sse(
                    "error",
                    {
                        "type": "error",
                        "code": "rate_limit_exceeded",
                        "message": "retry later",
                    },
                )
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=stream,
                    request=request,
                )
            if failure_kind == "responses_server_error":
                stream = _typed_sse(
                    "response.failed",
                    {
                        "type": "response.failed",
                        "response": {
                            "status": "failed",
                            "error": {"code": "server_error", "message": "retry later"},
                        },
                    },
                )
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=stream,
                    request=request,
                )
            if failure_kind == "messages_overloaded":
                stream = _typed_sse(
                    "error",
                    {
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "retry later"},
                    },
                )
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=stream,
                    request=request,
                )
            if failure_kind in {
                "messages_rate_limit",
                "messages_api_error",
                "messages_timeout_error",
            }:
                error_type = {
                    "messages_rate_limit": "rate_limit_error",
                    "messages_api_error": "api_error",
                    "messages_timeout_error": "timeout_error",
                }[failure_kind]
                stream = _typed_sse(
                    "error",
                    {
                        "type": "error",
                        "error": {"type": error_type, "message": "retry later"},
                    },
                )
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=stream,
                    request=request,
                )
            return httpx.Response(
                529,
                json={"type": "error", "error": {"type": "overloaded_error"}},
                request=request,
            )

        if failure_kind.startswith("responses_"):
            return httpx.Response(
                200,
                json={
                    "id": "response-retried",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "A"}],
                        }
                    ],
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "id": "message-retried",
                "type": "message",
                "content": [{"type": "text", "text": "A"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
            request=request,
        )

    async def no_sleep(_delay: float) -> None:
        return None

    adapter_class = (
        OpenAIResponsesAdapter
        if failure_kind.startswith("responses_")
        else AnthropicMessagesAdapter
    )
    generation = {} if failure_kind.startswith("responses_") else {"max_tokens": 16}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await adapter_class(
            "https://provider.example/v1",
            "remote-model",
            api_key="key",
            client=client,
            max_retries=1,
            sleep=no_sleep,
            attempt_controller=controller,
        ).generate(
            [{"role": "user", "content": "question"}],
            generation,
            attempt_context=_attempt_context(),
        )

    assert calls == 2
    assert result.metadata["attempts"] == 2
    assert controller.finishes == [
        (
            1,
            ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
            ProviderAttemptOutcome.HTTP_ERROR,
            None,
            None,
        ),
        (
            2,
            ProviderAttemptDisposition.SETTLED_ACTUAL,
            ProviderAttemptOutcome.SUCCEEDED,
            2,
            1,
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_class", "generation"),
    [
        (OpenAIResponsesAdapter, {}),
        (AnthropicMessagesAdapter, {"max_tokens": 16}),
    ],
)
async def test_unknown_typed_stream_error_fails_closed_without_retry(
    adapter_class: type,
    generation: dict[str, object],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        stream = _typed_sse(
            "error",
            {
                "type": "error",
                "error": {"type": "unknown_error", "message": "do not retry"},
            },
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stream,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = adapter_class(
            "https://provider.example/v1",
            "remote-model",
            api_key="key",
            client=client,
            max_retries=1,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate(
                [{"role": "user", "content": "question"}],
                generation,
            )

    assert calls == 1
    assert caught.value.error_type == "provider_stream_error"
    assert caught.value.retryable is False
