from __future__ import annotations

import asyncio
import json
import traceback

import httpx
import pytest

import app.adapters.openai_compatible as openai_compatible_module
from app.adapters import (
    AdapterError,
    MockModelAdapter,
    OpenAICompatibleAdapter,
    ProviderAttemptContext,
    ProviderAttemptDisposition,
    ProviderAttemptOutcome,
    ProviderAttemptPermit,
    ProviderAttemptStateUnknown,
    build_adapter,
)


def _attempt_context(
    question_id: str = "question-1",
    *,
    next_provider_attempt: int = 1,
) -> ProviderAttemptContext:
    return ProviderAttemptContext(
        run_id="run-1",
        question_id=question_id,
        model_id="model-1",
        provider_scope="provider-scope-1",
        lease_token=7,
        execution_generation=2,
        next_provider_attempt=next_provider_attempt,
        reserved_input_tokens=16,
        reserved_output_tokens=8,
    )


class _RecordingAttemptController:
    def __init__(
        self,
        *,
        events: list[tuple[object, ...]] | None = None,
        mark_error: BaseException | None = None,
        finish_error_at: int | None = None,
    ) -> None:
        self.events = events if events is not None else []
        self.contexts: list[ProviderAttemptContext] = []
        self.mark_error = mark_error
        self.finish_error_at = finish_error_at
        self.finish_calls = 0

    async def reserve(
        self,
        context: ProviderAttemptContext,
        *,
        provider_attempt: int,
    ) -> ProviderAttemptPermit:
        self.contexts.append(context)
        permit = ProviderAttemptPermit(
            reservation_id=f"reservation-{context.question_id}-{provider_attempt}",
            provider_attempt=provider_attempt,
        )
        self.events.append(("reserve", context.question_id, provider_attempt))
        return permit

    async def mark_send_started(self, permit: ProviderAttemptPermit) -> None:
        self.events.append(("mark", permit.reservation_id, permit.provider_attempt))
        if self.mark_error is not None:
            raise self.mark_error

    async def finish(
        self,
        permit: ProviderAttemptPermit,
        *,
        disposition: ProviderAttemptDisposition,
        outcome: ProviderAttemptOutcome,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self.finish_calls += 1
        self.events.append(
            (
                "finish",
                permit.reservation_id,
                permit.provider_attempt,
                disposition,
                outcome,
                input_tokens,
                output_tokens,
            )
        )
        if self.finish_error_at == self.finish_calls:
            raise RuntimeError("settlement outcome is unknown")


class _CancellingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        del request
        self.calls += 1
        raise asyncio.CancelledError


class _TrackedAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.index = 0
        self.yielded = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self.index >= len(self.chunks):
            raise StopAsyncIteration
        chunk = self.chunks[self.index]
        self.index += 1
        self.yielded += 1
        return chunk

    async def aclose(self) -> None:
        self.closed = True


class _FailingAsyncByteStream(_TrackedAsyncByteStream):
    def __init__(self, chunks: list[bytes], error: httpx.TransportError) -> None:
        super().__init__(chunks)
        self.error = error

    async def __anext__(self) -> bytes:
        try:
            return await super().__anext__()
        except StopAsyncIteration:
            raise self.error from None


class _YieldingAsyncByteStream(_TrackedAsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        super().__init__(chunks)

    async def __anext__(self) -> bytes:
        if self.index >= len(self.chunks):
            raise StopAsyncIteration
        await asyncio.sleep(0)
        chunk = self.chunks[self.index]
        self.index += 1
        self.yielded += 1
        return chunk


def _sse_event(body: object, *, ending: bytes = b"\n\n") -> bytes:
    return b"data: " + json.dumps(body, ensure_ascii=False).encode() + ending


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
async def test_mock_adapter_uses_one_synthetic_governed_attempt() -> None:
    controller = _RecordingAttemptController()
    context = _attempt_context(next_provider_attempt=3)

    result = await MockModelAdapter(attempt_controller=controller).generate(
        [],
        {
            "mock_response": "A",
            "mock_input_tokens": 4,
            "mock_output_tokens": 1,
        },
        attempt_context=context,
    )

    assert result.text == "A"
    assert controller.contexts == [context]
    assert controller.events == [
        ("reserve", "question-1", 3),
        ("mark", "reservation-question-1-3", 3),
        (
            "finish",
            "reservation-question-1-3",
            3,
            ProviderAttemptDisposition.SETTLED_ACTUAL,
            ProviderAttemptOutcome.SUCCEEDED,
            4,
            1,
        ),
    ]


@pytest.mark.asyncio
async def test_mock_adapter_conservatively_settles_configured_failure() -> None:
    controller = _RecordingAttemptController()

    with pytest.raises(AdapterError, match="try later"):
        await MockModelAdapter(attempt_controller=controller).generate(
            [],
            {"mock_error": {"type": "rate_limited", "message": "try later"}},
            attempt_context=_attempt_context(),
        )

    assert controller.events == [
        ("reserve", "question-1", 1),
        ("mark", "reservation-question-1-1", 1),
        (
            "finish",
            "reservation-question-1-1",
            1,
            ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
            ProviderAttemptOutcome.PROVIDER_RESPONSE_ERROR,
            None,
            None,
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "generation_config",
    [
        {"mock_generation_delay_seconds": True},
        {"mock_generation_delay_seconds": float("inf")},
        {"mock_latency_ms": True},
        {"mock_latency_ms": -0.1},
        {"mock_input_tokens": -1},
        {"mock_output_tokens": 1.5},
        {"mock_usage": []},
        {
            "mock_error": {"type": "rate_limited", "message": "try later"},
            "mock_latency_ms": float("nan"),
        },
    ],
)
async def test_mock_adapter_rejects_invalid_local_config_before_governance_reservation(
    generation_config: dict[str, object],
) -> None:
    controller = _RecordingAttemptController()

    with pytest.raises(AdapterError) as caught:
        await MockModelAdapter(attempt_controller=controller).generate(
            [],
            generation_config,
            attempt_context=_attempt_context(),
        )

    assert caught.value.error_type == "mock_configuration_error"
    assert controller.contexts == []
    assert controller.events == []
    assert controller.finish_calls == 0


@pytest.mark.asyncio
async def test_openai_compatible_sends_chat_completion_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "top-secret-key")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["accept"] = request.headers.get("accept")
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
    assert seen["accept"] == "text/event-stream"
    assert seen["accept_encoding"] == "identity"
    assert seen["payload"] == {
        "model": "remote-model",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hi"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
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
    assert result.metadata["response_mode"] == "json"


@pytest.mark.asyncio
async def test_openai_compatible_consumes_llamacpp_sse_through_usage_and_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sse-secret-token"
    monkeypatch.setenv("TEST_PROVIDER_KEY", secret)
    first_event = (
        b'\xef\xbb\xbfdata: {"id":"stream-123",\r\n'
        b'data: "model":"model","system_fingerprint":"fp-sse",'
        b'"choices":[{"index":0,"delta":{"role":"assistant",'
        b'"content":null},"finish_reason":null}]}\r\n\r\n'
    )
    payload = b"".join(
        [
            first_event,
            b": llama.cpp keepalive\n\n",
            _sse_event(
                {
                    "id": "stream-123",
                    "model": "model",
                    "system_fingerprint": "fp-sse",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"reasoning_content": "ignored reasoning"},
                            "finish_reason": None,
                        }
                    ],
                    "timings": {"predicted_per_second": 8.7},
                }
            ),
            _sse_event(
                {
                    "id": "stream-123",
                    "model": "model",
                    "system_fingerprint": "fp-sse",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "答案:sse-secret-"},
                            "finish_reason": None,
                        }
                    ],
                },
                ending=b"\r\n\r\n",
            ),
            _sse_event(
                {
                    "id": "stream-123",
                    "model": "model",
                    "system_fingerprint": "fp-sse",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "token\nAnswer: A"},
                            "finish_reason": None,
                        }
                    ],
                }
            ),
            _sse_event(
                {
                    "id": "stream-123",
                    "model": "model",
                    "system_fingerprint": "fp-sse",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
                ending=b"\r\r",
            ),
            _sse_event(
                {
                    "id": "stream-123",
                    "model": "model",
                    "system_fingerprint": "fp-sse",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 17,
                        "completion_tokens": 23,
                        "total_tokens": 40,
                    },
                    "timings": {"predicted_per_second": 8.7},
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    chinese_split = payload.index("答".encode()) + 1
    stream = _TrackedAsyncByteStream(
        [
            payload[:1],
            payload[1:chinese_split],
            payload[chinese_split : chinese_split + 2],
            payload[chinese_split + 2 : -5],
            payload[-5:],
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            client=client,
        ).generate([{"role": "user", "content": "question"}], {})

    assert result.text == "答案:[REDACTED]\nAnswer: A"
    assert result.input_tokens == 17
    assert result.output_tokens == 23
    assert result.raw_usage == {
        "prompt_tokens": 17,
        "completion_tokens": 23,
        "total_tokens": 40,
    }
    assert result.provider_request_id == "stream-123"
    assert result.metadata == {
        "adapter": "openai_compatible",
        "attempts": 1,
        "response_mode": "sse",
        "finish_reason": "stop",
        "returned_model": "model",
        "system_fingerprint": "fp-sse",
    }
    assert secret not in repr(result)
    assert stream.yielded == len(stream.chunks)
    assert stream.closed is True


@pytest.mark.asyncio
async def test_openai_compatible_retries_transport_failure_during_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    calls = 0
    streams: list[_TrackedAsyncByteStream] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            stream: _TrackedAsyncByteStream = _FailingAsyncByteStream(
                [
                    _sse_event(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "discarded partial"},
                                    "finish_reason": None,
                                }
                            ]
                        }
                    )
                ],
                httpx.ReadError("stream disconnected"),
            )
        else:
            stream = _TrackedAsyncByteStream(
                [
                    _sse_event(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "complete"},
                                    "finish_reason": "stop",
                                }
                            ]
                        }
                    ),
                    b"data: [DONE]\n\n",
                ]
            )
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            max_retries=1,
            retry_backoff_base_seconds=0,
            client=client,
        ).generate([{"role": "user", "content": "question"}], {})

    assert calls == 2
    assert result.text == "complete"
    assert result.metadata["attempts"] == 2
    assert all(stream.closed for stream in streams)


@pytest.mark.asyncio
async def test_openai_compatible_rejects_sse_without_done_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "partial-secret"
    monkeypatch.setenv("TEST_PROVIDER_KEY", secret)
    calls = 0
    stream = _TrackedAsyncByteStream(
        [
            _sse_event(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"partial {secret}"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
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
            await adapter.generate([{"role": "user", "content": "question"}], {})

    assert calls == 1
    assert caught.value.error_type == "incomplete_provider_stream"
    assert caught.value.attempts == 1
    assert secret not in repr(caught.value)
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "expected_error"),
    [
        (b"data: {not-json}\n\n", "invalid_provider_stream"),
        (
            b'data: {"choices":[{"index":0,"delta":{"content":"\\ud800"},'
            b'"finish_reason":null}]}\n\n',
            "invalid_provider_stream",
        ),
        (
            _sse_event({"error": {"message": "Authorization: Bearer stream-error-secret"}}),
            "provider_stream_error",
        ),
    ],
)
async def test_openai_compatible_rejects_invalid_or_error_sse_events(
    monkeypatch: pytest.MonkeyPatch,
    event: bytes,
    expected_error: str,
) -> None:
    secret = "stream-error-secret"
    monkeypatch.setenv("TEST_PROVIDER_KEY", secret)
    stream = _TrackedAsyncByteStream([event])
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            client=client,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "question"}], {})

    assert caught.value.error_type == expected_error
    assert caught.value.attempts == 1
    assert secret not in repr(caught.value)
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_kind", ["wire", "event", "content"])
async def test_openai_compatible_enforces_sse_size_limits(
    monkeypatch: pytest.MonkeyPatch,
    limit_kind: str,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    monkeypatch.setattr(openai_compatible_module, "MAX_CHAT_STREAM_WIRE_BYTES", 1024)
    monkeypatch.setattr(openai_compatible_module, "MAX_CHAT_STREAM_EVENT_BYTES", 1024)
    monkeypatch.setattr(openai_compatible_module, "MAX_CHAT_SUCCESS_RESPONSE_BYTES", 1024)
    if limit_kind == "wire":
        monkeypatch.setattr(openai_compatible_module, "MAX_CHAT_STREAM_WIRE_BYTES", 16)
        chunks = [b": keepalive data that exceeds the wire cap\n\n"]
        expected_limit = 16
    elif limit_kind == "event":
        monkeypatch.setattr(openai_compatible_module, "MAX_CHAT_STREAM_EVENT_BYTES", 8)
        chunks = [b"data: {}\n\n"]
        expected_limit = 8
    else:
        monkeypatch.setattr(openai_compatible_module, "MAX_CHAT_SUCCESS_RESPONSE_BYTES", 4)
        chunks = [
            _sse_event(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "12345"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ),
            b"data: [DONE]\n\n",
        ]
        expected_limit = 4
    stream = _TrackedAsyncByteStream(chunks)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            max_retries=2,
            retry_backoff_base_seconds=0,
            client=client,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "question"}], {})

    assert caught.value.error_type == "provider_response_too_large"
    assert caught.value.attempts == 1
    assert f"{expected_limit}-byte safety limit" in caught.value.error_message
    assert stream.closed is True


@pytest.mark.asyncio
async def test_openai_compatible_keeps_concurrent_sse_state_request_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    streams: list[_TrackedAsyncByteStream] = []
    controller = _RecordingAttemptController()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        label = payload["messages"][-1]["content"]
        complete = b"".join(
            [
                _sse_event(
                    {
                        "id": f"request-{label}",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": f"answer-{label}"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": len(label)},
                    }
                ),
                b"data: [DONE]\n\n",
            ]
        )
        midpoint = len(complete) // 2
        stream = _YieldingAsyncByteStream([complete[:midpoint], complete[midpoint:]])
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            client=client,
            attempt_controller=controller,
        )
        first, second = await asyncio.gather(
            adapter.generate(
                [{"role": "user", "content": "first"}],
                {},
                attempt_context=_attempt_context("question-first"),
            ),
            adapter.generate(
                [{"role": "user", "content": "second"}],
                {},
                attempt_context=_attempt_context("question-second"),
            ),
        )

    assert (first.text, first.provider_request_id, first.output_tokens) == (
        "answer-first",
        "request-first",
        5,
    )
    assert (second.text, second.provider_request_id, second.output_tokens) == (
        "answer-second",
        "request-second",
        6,
    )
    assert len(streams) == 2
    assert all(stream.closed for stream in streams)
    assert {context.question_id for context in controller.contexts} == {
        "question-first",
        "question-second",
    }
    finished_reservations = {event[1] for event in controller.events if event[0] == "finish"}
    assert finished_reservations == {
        "reservation-question-first-1",
        "reservation-question-second-1",
    }


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
async def test_openai_compatible_classifies_empty_sse_after_length_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    stream = _TrackedAsyncByteStream(
        [
            _sse_event(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "length",
                        }
                    ]
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            client=client,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "question"}], {})

    assert caught.value.error_type == "output_truncated"
    assert "Increase max_tokens" in caught.value.error_message
    assert stream.closed is True


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
async def test_openai_governance_settles_each_attempt_before_retry_and_hides_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "governance-boundary-secret"
    monkeypatch.setenv("TEST_PROVIDER_KEY", secret)
    statuses = iter([429, 503, 200])
    events: list[tuple[object, ...]] = []
    controller = _RecordingAttemptController(events=events)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {secret}"
        status = next(statuses)
        events.append(("http", status))
        if status == 200:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                },
            )
        return httpx.Response(status, json={"error": {"message": "temporarily unavailable"}})

    async def fake_sleep(delay: float) -> None:
        events.append(("sleep", delay))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            max_retries=2,
            retry_backoff_base_seconds=0.1,
            client=client,
            sleep=fake_sleep,
            attempt_controller=controller,
        ).generate(
            [{"role": "user", "content": "hello"}],
            {},
            attempt_context=_attempt_context(),
        )

    assert result.text == "ok"
    assert result.metadata["attempts"] == 3
    assert events == [
        ("reserve", "question-1", 1),
        ("mark", "reservation-question-1-1", 1),
        ("http", 429),
        (
            "finish",
            "reservation-question-1-1",
            1,
            ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
            ProviderAttemptOutcome.HTTP_ERROR,
            None,
            None,
        ),
        ("sleep", 0.1),
        ("reserve", "question-1", 2),
        ("mark", "reservation-question-1-2", 2),
        ("http", 503),
        (
            "finish",
            "reservation-question-1-2",
            2,
            ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
            ProviderAttemptOutcome.HTTP_ERROR,
            None,
            None,
        ),
        ("sleep", 0.2),
        ("reserve", "question-1", 3),
        ("mark", "reservation-question-1-3", 3),
        ("http", 200),
        (
            "finish",
            "reservation-question-1-3",
            3,
            ProviderAttemptDisposition.SETTLED_ACTUAL,
            ProviderAttemptOutcome.SUCCEEDED,
            3,
            2,
        ),
    ]
    assert secret not in repr(controller.contexts)
    assert secret not in repr(controller.events)
    for context in controller.contexts:
        assert not hasattr(context, "api_key")
        assert not hasattr(context, "headers")
        assert not hasattr(context, "body")
        assert not hasattr(context, "raw_usage")


@pytest.mark.asyncio
async def test_openai_governance_keeps_known_usage_and_conservatively_settles_missing_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    controller = _RecordingAttemptController()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3},
            },
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        result = await OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            client=client,
            attempt_controller=controller,
        ).generate(
            [{"role": "user", "content": "hello"}],
            {},
            attempt_context=_attempt_context(),
        )

    assert result.input_tokens == 3
    assert result.output_tokens is None
    assert controller.events[-1] == (
        "finish",
        "reservation-question-1-1",
        1,
        ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
        ProviderAttemptOutcome.USAGE_INCOMPLETE,
        3,
        None,
    )


@pytest.mark.asyncio
async def test_openai_governance_resumes_from_persisted_provider_attempt_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    statuses = iter([503, 200])
    controller = _RecordingAttemptController()

    def handler(_request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status == 200:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                },
            )
        return httpx.Response(503, json={"error": {"message": "retry"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            max_retries=2,
            retry_backoff_base_seconds=0,
            client=client,
            attempt_controller=controller,
        ).generate(
            [{"role": "user", "content": "hello"}],
            {},
            attempt_context=_attempt_context(next_provider_attempt=2),
        )

    assert result.metadata["attempts"] == 3
    assert [event[2] for event in controller.events if event[0] == "reserve"] == [2, 3]


@pytest.mark.asyncio
async def test_openai_governance_preflight_failures_do_not_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_key_controller = _RecordingAttemptController()
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)
    missing_key_adapter = OpenAICompatibleAdapter(
        "https://provider.example/v1",
        "model",
        "MISSING_PROVIDER_KEY",
        attempt_controller=missing_key_controller,
    )
    with pytest.raises(AdapterError, match="is not set"):
        await missing_key_adapter.generate(
            [{"role": "user", "content": "hello"}],
            {},
            attempt_context=_attempt_context(),
        )
    assert missing_key_controller.events == []

    invalid_payload_controller = _RecordingAttemptController()
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    invalid_payload_adapter = OpenAICompatibleAdapter(
        "https://provider.example/v1",
        "model",
        "TEST_PROVIDER_KEY",
        attempt_controller=invalid_payload_controller,
    )
    with pytest.raises(AdapterError, match="At least one message"):
        await invalid_payload_adapter.generate(
            [],
            {},
            attempt_context=_attempt_context(),
        )
    assert invalid_payload_controller.events == []


@pytest.mark.asyncio
async def test_openai_governance_requires_context_and_controller_as_a_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    controller = _RecordingAttemptController()
    with_controller = OpenAICompatibleAdapter(
        "https://provider.example/v1",
        "model",
        "TEST_PROVIDER_KEY",
        attempt_controller=controller,
    )
    with pytest.raises(ValueError, match="attempt_context is required"):
        await with_controller.generate([{"role": "user", "content": "hello"}], {})
    assert controller.events == []

    without_controller = OpenAICompatibleAdapter(
        "https://provider.example/v1",
        "model",
        "TEST_PROVIDER_KEY",
    )
    with pytest.raises(ValueError, match="requires an attempt_controller"):
        await without_controller.generate(
            [{"role": "user", "content": "hello"}],
            {},
            attempt_context=_attempt_context(),
        )


@pytest.mark.asyncio
async def test_openai_governance_releases_when_mark_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    controller = _RecordingAttemptController(mark_error=RuntimeError("mark failed"))
    http_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "A"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            client=client,
            attempt_controller=controller,
        )
        with pytest.raises(RuntimeError, match="mark failed"):
            await adapter.generate(
                [{"role": "user", "content": "hello"}],
                {},
                attempt_context=_attempt_context(),
            )

    assert http_calls == 0
    assert controller.events[-1] == (
        "finish",
        "reservation-question-1-1",
        1,
        ProviderAttemptDisposition.RELEASED_PRE_SEND,
        ProviderAttemptOutcome.MARK_SEND_FAILED,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_openai_governance_leaves_cancelled_send_start_for_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    controller = _RecordingAttemptController(mark_error=asyncio.CancelledError())
    http_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "A"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            client=client,
            attempt_controller=controller,
        )
        with pytest.raises(asyncio.CancelledError):
            await adapter.generate(
                [{"role": "user", "content": "hello"}],
                {},
                attempt_context=_attempt_context(),
            )

    assert http_calls == 0
    assert [event[0] for event in controller.events] == ["reserve", "mark"]


@pytest.mark.asyncio
async def test_openai_governance_unknown_send_start_is_never_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    controller = _RecordingAttemptController(
        mark_error=ProviderAttemptStateUnknown("commit acknowledgement lost")
    )
    http_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "A"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            client=client,
            attempt_controller=controller,
        )
        with pytest.raises(ProviderAttemptStateUnknown):
            await adapter.generate(
                [{"role": "user", "content": "hello"}],
                {},
                attempt_context=_attempt_context(),
            )

    assert http_calls == 0
    assert [event[0] for event in controller.events] == ["reserve", "mark"]


@pytest.mark.asyncio
async def test_openai_governance_conservatively_settles_cancellation_after_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    controller = _RecordingAttemptController()
    transport = _CancellingTransport()

    async with httpx.AsyncClient(transport=transport) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            client=client,
            attempt_controller=controller,
        )
        with pytest.raises(asyncio.CancelledError):
            await adapter.generate(
                [{"role": "user", "content": "hello"}],
                {},
                attempt_context=_attempt_context(),
            )

    assert transport.calls == 1
    assert controller.events[-1] == (
        "finish",
        "reservation-question-1-1",
        1,
        ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
        ProviderAttemptOutcome.CANCELLED,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_openai_governance_unknown_settlement_stops_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    controller = _RecordingAttemptController(finish_error_at=1)
    http_calls = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(429, json={"error": {"message": "retry"}})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            max_retries=2,
            client=client,
            sleep=fake_sleep,
            attempt_controller=controller,
        )
        with pytest.raises(RuntimeError, match="settlement outcome is unknown"):
            await adapter.generate(
                [{"role": "user", "content": "hello"}],
                {},
                attempt_context=_attempt_context(),
            )

    assert http_calls == 1
    assert delays == []
    assert [event[0] for event in controller.events] == ["reserve", "mark", "finish"]


@pytest.mark.asyncio
async def test_openai_governance_conservatively_settles_transport_failure_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    controller = _RecordingAttemptController()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connection failed", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "model",
            "TEST_PROVIDER_KEY",
            max_retries=1,
            retry_backoff_base_seconds=0,
            client=client,
            attempt_controller=controller,
        ).generate(
            [{"role": "user", "content": "hello"}],
            {},
            attempt_context=_attempt_context(),
        )

    assert result.text == "ok"
    finishes = [event for event in controller.events if event[0] == "finish"]
    assert finishes[0][3:5] == (
        ProviderAttemptDisposition.SETTLED_CONSERVATIVE,
        ProviderAttemptOutcome.TRANSPORT_ERROR,
    )
    assert finishes[1][3:5] == (
        ProviderAttemptDisposition.SETTLED_ACTUAL,
        ProviderAttemptOutcome.SUCCEEDED,
    )


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
    secret = "transport-exception-canary-key"
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
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    formatted_traceback = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert secret not in formatted_traceback


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
    controller = _RecordingAttemptController()
    assert isinstance(build_adapter("mock", base_url="ignored"), MockModelAdapter)
    governed_mock = build_adapter(
        "mock",
        base_url="ignored",
        attempt_controller=controller,
    )
    governed_openai = build_adapter(
        "openai_compatible",
        base_url="https://provider.example/v1",
        remote_model_name="model",
        api_key_env="KEY",
        connect_timeout=None,
        attempt_controller=controller,
    )
    assert isinstance(governed_mock, MockModelAdapter)
    assert isinstance(governed_openai, OpenAICompatibleAdapter)
    assert governed_mock._attempt_controller is controller  # type: ignore[attr-defined]
    assert governed_openai._attempt_controller is controller  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="Unsupported provider_type"):
        build_adapter("unknown")


@pytest.mark.asyncio
async def test_adapter_registry_forwards_only_supported_mock_hooks() -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    adapter = build_adapter(
        "mock",
        sleep=fake_sleep,
        base_url="ignored",
        api_key="ignored",
    )
    result = await adapter.generate(
        [],
        {"mock_response": "A", "mock_generation_delay_seconds": 0.25},
    )

    assert result.text == "A"
    assert delays == [0.25]


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
