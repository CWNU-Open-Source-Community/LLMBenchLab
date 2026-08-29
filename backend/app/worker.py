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

# This module is executed as ``python -m app.worker`` in production, where
# ``__name__`` is ``__main__``. Keep the registered application logger stable
# so the final formatter preserves the closed Worker event contract.
logger = logging.getLogger("app.worker")


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
        "Worker process is starting",
        extra={"event": "worker_starting", "worker_id": service.worker_id},
    )
    try:
        try:
            await service.run(stop)
        except Exception as exc:
            logger.error(
                "Worker main service stopped before a healthy lifecycle completed",
                extra={
                    "event": "worker_service_failed",
                    "error_code": f"worker_service_error:{type(exc).__name__}",
                    "result": "stopped",
                },
            )
            return_code = 1
        else:
            return_code = 0
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
    return return_code


def main() -> None:
    raise SystemExit(asyncio.run(_run_worker()))


if __name__ == "__main__":
    main()
