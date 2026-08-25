"""CLI entry point for the independent LLMBenchLab Worker process."""

from __future__ import annotations

import asyncio
import logging
import signal

from app.core.config import get_settings
from app.db.init_db import initialize_database
from app.db.session import SessionLocal, engine
from app.task_queue import RedisRunQueue
from app.workers import WorkerService

logger = logging.getLogger(__name__)


async def _run_worker() -> int:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    try:
        initialize_database()
    except Exception:
        logger.error("Worker startup database check failed", extra={"event": "worker_start_failed"})
        return 1

    run_queue = (
        RedisRunQueue.from_url(
            settings.redis_url,
            stream=settings.task_stream,
            consumer_group=settings.task_consumer_group,
            max_length=settings.task_stream_max_length,
            block_milliseconds=settings.redis_block_milliseconds,
            max_connections=settings.redis_max_connections,
            publish_timeout_seconds=settings.redis_publish_timeout_seconds,
            operation_timeout_seconds=settings.redis_operation_timeout_seconds,
        )
        if settings.redis_url is not None
        else None
    )
    service = WorkerService(SessionLocal, settings, run_queue=run_queue)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
        except NotImplementedError:
            continue
        installed_signals.append(signal_name)
    logger.info(
        "Worker started",
        extra={"event": "worker_started", "worker_id": service.worker_id},
    )
    try:
        await service.run(stop)
    finally:
        for signal_name in installed_signals:
            loop.remove_signal_handler(signal_name)
        await service.close()
        engine.dispose()
    logger.info(
        "Worker stopped",
        extra={"event": "worker_stopped", "worker_id": service.worker_id},
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run_worker()))


if __name__ == "__main__":
    main()
