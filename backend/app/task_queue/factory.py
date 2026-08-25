"""Task queue construction shared by API, Worker, and health probes."""

from app.core.config import Settings
from app.task_queue.redis_streams import RedisRunQueue


def create_run_queue(settings: Settings) -> RedisRunQueue | None:
    if settings.redis_url is None:
        return None
    return RedisRunQueue.from_url(
        settings.redis_url,
        stream=settings.task_stream,
        consumer_group=settings.task_consumer_group,
        max_length=settings.task_stream_max_length,
        block_milliseconds=settings.redis_block_milliseconds,
        max_connections=settings.redis_max_connections,
        publish_timeout_seconds=settings.redis_publish_timeout_seconds,
        operation_timeout_seconds=settings.redis_operation_timeout_seconds,
    )
