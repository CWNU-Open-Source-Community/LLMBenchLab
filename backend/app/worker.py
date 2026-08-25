"""CLI entry point for the independent LLMBenchLab Worker process."""

from __future__ import annotations

import asyncio
import logging
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.init_db import initialize_database
from app.db.session import SessionLocal, engine
from app.task_queue import create_run_queue
from app.workers import WorkerService

logger = logging.getLogger(__name__)


async def _run_worker() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        initialize_database()
    except Exception:
        logger.error("Worker startup database check failed", extra={"event": "worker_start_failed"})
        engine.dispose()
        return 1

    run_queue = create_run_queue(settings)
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
        try:
            await service.close()
        finally:
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
