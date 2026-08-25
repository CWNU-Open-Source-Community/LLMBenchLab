"""Redis Streams command and message-contract unit tests."""

from __future__ import annotations

import asyncio

import pytest
from redis.exceptions import ResponseError

from app.task_queue import TASK_MESSAGE_VERSION, QueueUnavailable, RedisRunQueue


class _FakeRedis:
    def __init__(self) -> None:
        self.added = []
        self.group_calls = []
        self.read_response = []
        self.claim_response = ["0-0", [], []]
        self.acked = []
        self.closed = False
        self.group_error: ResponseError | None = None
        self.block_publish = False
        self.block_read = False

    async def xgroup_create(self, *args, **kwargs):
        self.group_calls.append((args, kwargs))
        if self.group_error is not None:
            raise self.group_error
        return True

    async def xadd(self, *args, **kwargs):
        self.added.append((args, kwargs))
        if self.block_publish:
            await asyncio.Event().wait()
        return "1-0"

    async def xreadgroup(self, *args, **kwargs):
        del args, kwargs
        if self.block_read:
            await asyncio.Event().wait()
        return self.read_response

    async def xautoclaim(self, *args, **kwargs):
        del args, kwargs
        return self.claim_response

    async def xack(self, *args):
        self.acked.append(args)
        return 1

    async def ping(self):
        return True

    async def aclose(self):
        self.closed = True


def _queue(client: _FakeRedis, *, publish_timeout: float = 1.0) -> RedisRunQueue:
    return RedisRunQueue(
        client,  # type: ignore[arg-type]
        stream="runs",
        consumer_group="workers",
        max_length=100,
        default_block_milliseconds=1000,
        publish_timeout_seconds=publish_timeout,
        operation_timeout_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_publish_uses_versioned_bounded_stream_message() -> None:
    client = _FakeRedis()
    queue = _queue(client)

    message_id = await queue.publish("run-1", correlation_id="correlation-1")

    assert message_id == "1-0"
    assert client.added == [
        (
            (
                "runs",
                {
                    "version": TASK_MESSAGE_VERSION,
                    "run_id": "run-1",
                    "correlation_id": "correlation-1",
                },
            ),
            {"maxlen": 100, "approximate": True},
        )
    ]


@pytest.mark.asyncio
async def test_publish_timeout_is_sanitized_and_bounded() -> None:
    client = _FakeRedis()
    client.block_publish = True
    queue = _queue(client, publish_timeout=0.01)

    with pytest.raises(QueueUnavailable, match="queue_publish_unavailable"):
        await queue.publish("run-1", correlation_id="correlation-1")


@pytest.mark.asyncio
async def test_half_open_read_is_sanitized_and_bounded() -> None:
    client = _FakeRedis()
    client.block_read = True
    queue = _queue(client)

    with pytest.raises(QueueUnavailable, match="queue_read_unavailable"):
        await queue.read_new(consumer="worker-1", block_milliseconds=0)


@pytest.mark.asyncio
async def test_group_creation_starts_at_beginning_and_tolerates_existing_group() -> None:
    client = _FakeRedis()
    queue = _queue(client)
    await queue.ensure_consumer_group()
    assert client.group_calls == [(("runs", "workers"), {"id": "0-0", "mkstream": True})]

    client.group_error = ResponseError("BUSYGROUP Consumer Group name already exists")
    await queue.ensure_consumer_group()


@pytest.mark.asyncio
async def test_read_claim_cursor_validation_ack_and_close() -> None:
    client = _FakeRedis()
    queue = _queue(client)
    client.read_response = [
        (
            "runs",
            [
                (
                    "2-0",
                    {
                        "version": TASK_MESSAGE_VERSION,
                        "run_id": "run-2",
                        "correlation_id": "correlation-2",
                    },
                )
            ],
        )
    ]
    delivery = await queue.read_new(consumer="worker-1", block_milliseconds=0)
    assert delivery is not None and delivery.is_valid
    assert delivery.message_id == "2-0"
    assert delivery.run_id == "run-2"

    client.claim_response = [
        "7-0",
        [
            (
                "3-0",
                {
                    "version": "unknown-version",
                    "run_id": "run-3",
                    "correlation_id": "correlation-3",
                },
            )
        ],
        [],
    ]
    cursor, invalid = await queue.claim_stale(
        consumer="worker-1",
        min_idle_milliseconds=30_000,
        start_id="2-0",
    )
    assert cursor == "7-0"
    assert invalid is not None and not invalid.is_valid
    assert await queue.ack(invalid.message_id) is True
    assert client.acked == [("runs", "workers", "3-0")]
    assert await queue.ping() is True
    await queue.close()
    assert client.closed is True
