"""Redis Streams as an at-least-once notification layer over database task truth."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

TASK_MESSAGE_VERSION = "llmbenchlab-run-task-v1"


class QueueUnavailable(RuntimeError):
    """A sanitized Redis failure that is safe to surface in application control flow."""


@dataclass(frozen=True, slots=True)
class RunTaskDelivery:
    message_id: str
    run_id: str | None
    correlation_id: str | None

    @property
    def is_valid(self) -> bool:
        return self.run_id is not None and self.correlation_id is not None


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class RedisRunQueue:
    """Publish and consume lossy notifications without treating Redis as task state."""

    def __init__(
        self,
        client: Redis,
        *,
        stream: str,
        consumer_group: str,
        max_length: int,
        default_block_milliseconds: int,
        publish_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> None:
        self._client = client
        self.stream = stream
        self.consumer_group = consumer_group
        self._max_length = max_length
        self._default_block_milliseconds = default_block_milliseconds
        self._publish_timeout_seconds = publish_timeout_seconds
        self._operation_timeout_seconds = operation_timeout_seconds

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        stream: str,
        consumer_group: str,
        max_length: int,
        block_milliseconds: int,
        max_connections: int,
        publish_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> RedisRunQueue:
        client = Redis.from_url(
            url,
            decode_responses=True,
            encoding="utf-8",
            max_connections=max_connections,
            socket_connect_timeout=1.0,
            socket_timeout=max(2.0, block_milliseconds / 1000 + 1.0),
            health_check_interval=30,
            retry_on_timeout=False,
        )
        return cls(
            client,
            stream=stream,
            consumer_group=consumer_group,
            max_length=max_length,
            default_block_milliseconds=block_milliseconds,
            publish_timeout_seconds=publish_timeout_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        )

    async def ensure_consumer_group(self) -> None:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                await self._client.xgroup_create(
                    self.stream,
                    self.consumer_group,
                    id="0-0",
                    mkstream=True,
                )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise QueueUnavailable("queue_consumer_group_unavailable") from exc
        except (RedisError, OSError, TimeoutError) as exc:
            raise QueueUnavailable("queue_consumer_group_unavailable") from exc

    async def publish(self, run_id: str, *, correlation_id: str) -> str:
        if not 1 <= len(run_id) <= 128 or not 1 <= len(correlation_id) <= 128:
            raise ValueError("run_id and correlation_id must contain 1 to 128 characters")
        try:
            async with asyncio.timeout(self._publish_timeout_seconds):
                message_id = await self._client.xadd(
                    self.stream,
                    {
                        "version": TASK_MESSAGE_VERSION,
                        "run_id": run_id,
                        "correlation_id": correlation_id,
                    },
                    maxlen=self._max_length,
                    approximate=True,
                )
        except (RedisError, OSError, TimeoutError) as exc:
            raise QueueUnavailable("queue_publish_unavailable") from exc
        return _text(message_id)

    async def read_new(
        self,
        *,
        consumer: str,
        block_milliseconds: int | None = None,
    ) -> RunTaskDelivery | None:
        block = (
            self._default_block_milliseconds if block_milliseconds is None else block_milliseconds
        )
        try:
            timeout_seconds = self._operation_timeout_seconds + max(0, block) / 1000
            async with asyncio.timeout(timeout_seconds):
                response = await self._client.xreadgroup(
                    self.consumer_group,
                    consumer,
                    {self.stream: ">"},
                    count=1,
                    block=block if block > 0 else None,
                    noack=False,
                )
        except (RedisError, OSError, TimeoutError) as exc:
            raise QueueUnavailable("queue_read_unavailable") from exc
        return self._first_delivery(response)

    async def claim_stale(
        self,
        *,
        consumer: str,
        min_idle_milliseconds: int,
        start_id: str,
    ) -> tuple[str, RunTaskDelivery | None]:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                response = await self._client.xautoclaim(
                    self.stream,
                    self.consumer_group,
                    consumer,
                    min_idle_milliseconds,
                    start_id=start_id,
                    count=1,
                )
        except (RedisError, OSError, TimeoutError) as exc:
            raise QueueUnavailable("queue_claim_unavailable") from exc
        next_start_id = _text(response[0]) if response else "0-0"
        messages = response[1] if response and len(response) > 1 else []
        if not messages:
            return next_start_id, None
        message_id, fields = messages[0]
        return next_start_id, self._delivery(message_id, fields)

    async def ack(self, message_id: str) -> bool:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                acknowledged = await self._client.xack(
                    self.stream,
                    self.consumer_group,
                    message_id,
                )
        except (RedisError, OSError, TimeoutError) as exc:
            raise QueueUnavailable("queue_ack_unavailable") from exc
        return bool(acknowledged)

    async def ping(self) -> bool:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                return bool(await self._client.ping())
        except (RedisError, OSError, TimeoutError) as exc:
            raise QueueUnavailable("queue_ping_unavailable") from exc

    async def close(self) -> None:
        await self._client.aclose()

    def _first_delivery(self, response: Any) -> RunTaskDelivery | None:
        if not response:
            return None
        _stream, messages = response[0]
        if not messages:
            return None
        message_id, fields = messages[0]
        return self._delivery(message_id, fields)

    @staticmethod
    def _delivery(message_id: Any, fields: Mapping[Any, Any]) -> RunTaskDelivery:
        normalized = {_text(key): _text(value) for key, value in fields.items()}
        run_id = normalized.get("run_id")
        correlation_id = normalized.get("correlation_id")
        if (
            normalized.get("version") != TASK_MESSAGE_VERSION
            or run_id is None
            or not 1 <= len(run_id) <= 128
            or correlation_id is None
            or not 1 <= len(correlation_id) <= 128
        ):
            run_id = None
            correlation_id = None
        return RunTaskDelivery(
            message_id=_text(message_id),
            run_id=run_id,
            correlation_id=correlation_id,
        )
