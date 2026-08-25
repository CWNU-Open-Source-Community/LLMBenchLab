"""Independent Worker coordination over Redis notifications and database reconciliation."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import suppress
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.runners.evaluation_runner import EvaluationRunner
from app.runners.run_leases import ReapReport, RunLeaseRepository
from app.task_queue import QueueUnavailable, RedisRunQueue, RunTaskDelivery

logger = logging.getLogger(__name__)


def default_worker_id() -> str:
    host = socket.gethostname()[:40]
    return f"worker:{host}:{os.getpid()}:{uuid4()}"[:128]


class WorkerService:
    """Execute at most one Run while continuously reconciling database task truth."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        run_queue: RedisRunQueue | None,
        worker_id: str | None = None,
        lease_repository: RunLeaseRepository | None = None,
        runner: EvaluationRunner | None = None,
    ) -> None:
        self.worker_id = worker_id or default_worker_id()
        self._settings = settings
        self._run_queue = run_queue
        self._repository = lease_repository or RunLeaseRepository(
            session_factory,
            lease_for=timedelta(seconds=settings.worker_lease_seconds),
            retry_backoff_base=timedelta(seconds=settings.worker_retry_backoff_base_seconds),
            retry_backoff_cap=timedelta(seconds=settings.worker_retry_backoff_cap_seconds),
        )
        self._runner = runner or EvaluationRunner(
            session_factory,
            worker_id=self.worker_id,
            lease_repository=self._repository,
        )
        self._queue_initialized = False
        self._queue_available: bool | None = None
        self._autoclaim_cursor = "0-0"
        self._database_available: bool | None = None

    async def run_once(self) -> bool:
        """Reconcile leases and process at most one Run for deterministic tests/tools."""

        report = self._reap_expired()
        if report is None:
            return False
        run_ids = self._due_run_ids()
        if run_ids is None:
            return False
        if run_ids:
            await self._runner.execute(run_ids[0])
            return True
        delivery = await self._next_delivery(block_milliseconds=0)
        if delivery is not None:
            await self._process_delivery(delivery)
            return True
        return self._reaped_any(report)

    async def run(self, stop: asyncio.Event) -> None:
        """Run until stopped, then drain or cancel the current Run within the grace period."""

        active: asyncio.Task[bool] | None = None
        active_delivery: RunTaskDelivery | None = None
        try:
            while True:
                if active is not None:
                    if active.done():
                        await self._settle_active(active, active_delivery)
                        active = None
                        active_delivery = None
                        continue
                    if stop.is_set():
                        await self._shutdown_active(active, active_delivery)
                        return
                    await self._wait_for_active_or_stop(active, stop)
                    continue

                if stop.is_set():
                    return

                # Reaping is synchronous database work. Keep it out of the active-Run
                # path so a slow database cannot delay that Run's heartbeat or start
                # the graceful-shutdown clock after SIGTERM.
                report = self._reap_expired()
                await asyncio.sleep(0)
                if stop.is_set():
                    return
                if report is None:
                    await self._wait_for_stop(stop, self._settings.worker_poll_seconds)
                    continue

                run_ids = self._due_run_ids()
                await asyncio.sleep(0)
                if stop.is_set():
                    return
                if run_ids is None:
                    await self._wait_for_stop(stop, self._settings.worker_poll_seconds)
                    continue
                if run_ids:
                    active = asyncio.create_task(
                        self._runner.execute(run_ids[0], shutdown_requested=stop),
                        name=f"run-{run_ids[0]}",
                    )
                    continue

                block_milliseconds = min(
                    self._settings.redis_block_milliseconds,
                    max(1, round(self._settings.worker_poll_seconds * 1000)),
                )
                delivery = await self._next_delivery_until_stopped(
                    stop,
                    block_milliseconds=block_milliseconds,
                )
                if stop.is_set():
                    return
                if delivery is not None:
                    if not delivery.is_valid:
                        await self._process_delivery(delivery)
                        if stop.is_set():
                            return
                        continue
                    if stop.is_set():
                        return
                    active_delivery = delivery
                    active = asyncio.create_task(
                        self._runner.execute(
                            delivery.run_id or "",
                            shutdown_requested=stop,
                        ),
                        name=f"run-{delivery.run_id}",
                    )
                    continue

                if not self._reaped_any(report):
                    await self._wait_for_stop(stop, self._settings.worker_poll_seconds)
        finally:
            if active is not None and not active.done():
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)

    async def _next_delivery_until_stopped(
        self,
        stop: asyncio.Event,
        *,
        block_milliseconds: int,
    ) -> RunTaskDelivery | None:
        delivery_task = asyncio.create_task(
            self._next_delivery(block_milliseconds=block_milliseconds)
        )
        stop_task = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait(
                {delivery_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop.is_set():
                delivery_task.cancel()
                await asyncio.gather(delivery_task, return_exceptions=True)
                return None
            return await delivery_task
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            if not delivery_task.done():
                delivery_task.cancel()
                await asyncio.gather(delivery_task, return_exceptions=True)

    async def close(self) -> None:
        if self._run_queue is not None:
            await self._run_queue.close()

    async def _next_delivery(
        self,
        *,
        block_milliseconds: int,
    ) -> RunTaskDelivery | None:
        if self._run_queue is None:
            return None
        try:
            if not self._queue_initialized:
                await self._run_queue.ensure_consumer_group()
                self._queue_initialized = True
            self._autoclaim_cursor, delivery = await self._run_queue.claim_stale(
                consumer=self.worker_id,
                min_idle_milliseconds=max(
                    1,
                    round(self._settings.worker_lease_seconds * 1000),
                ),
                start_id=self._autoclaim_cursor,
            )
            if delivery is None:
                delivery = await self._run_queue.read_new(
                    consumer=self.worker_id,
                    block_milliseconds=block_milliseconds,
                )
        except QueueUnavailable:
            self._queue_failure("run_queue_read_failed")
            return None
        self._queue_recovered()
        return delivery

    async def _process_delivery(self, delivery: RunTaskDelivery) -> None:
        if not delivery.is_valid:
            logger.warning(
                "Discarding invalid Run queue notification",
                extra={
                    "event": "run_queue_invalid_message",
                    "worker_id": self.worker_id,
                    "message_id": delivery.message_id,
                },
            )
            await self._ack(delivery)
            return
        try:
            ack_safe = await self._runner.execute(delivery.run_id or "")
        except Exception:
            logger.error(
                "Worker Run task escaped its isolation boundary",
                extra={
                    "event": "worker_run_unhandled_error",
                    "worker_id": self.worker_id,
                    "run_id": delivery.run_id,
                },
            )
            return
        if ack_safe:
            await self._ack(delivery)

    async def _settle_active(
        self,
        active: asyncio.Task[bool],
        delivery: RunTaskDelivery | None,
    ) -> None:
        try:
            ack_safe = await active
        except asyncio.CancelledError:
            return
        except Exception:
            logger.error(
                "Worker Run task escaped its isolation boundary",
                extra={"event": "worker_run_unhandled_error", "worker_id": self.worker_id},
            )
            return
        if delivery is not None and ack_safe:
            await self._ack(delivery)

    async def _shutdown_active(
        self,
        active: asyncio.Task[bool],
        delivery: RunTaskDelivery | None,
    ) -> None:
        try:
            ack_safe = await asyncio.wait_for(
                asyncio.shield(active),
                timeout=self._settings.worker_shutdown_grace_seconds,
            )
        except TimeoutError:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            logger.warning(
                "Worker shutdown grace expired; active Run was interrupted",
                extra={"event": "worker_shutdown_interrupted", "worker_id": self.worker_id},
            )
            return
        except Exception:
            return
        if delivery is not None and ack_safe:
            await self._ack(delivery)

    async def _ack(self, delivery: RunTaskDelivery) -> None:
        if self._run_queue is None:
            return
        try:
            await self._run_queue.ack(delivery.message_id)
        except QueueUnavailable:
            self._queue_failure("run_queue_ack_failed")

    async def _wait_for_active_or_stop(
        self,
        active: asyncio.Task[bool],
        stop: asyncio.Event,
    ) -> None:
        stop_task = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait(
                {active, stop_task},
                timeout=self._settings.worker_poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)

    @staticmethod
    async def _wait_for_stop(stop: asyncio.Event, timeout: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=timeout)

    @staticmethod
    def _reaped_any(report: ReapReport) -> bool:
        return bool(report.cancelled or report.dead_lettered or report.completed)

    def _queue_failure(self, event: str) -> None:
        if self._queue_available is not False:
            logger.warning(
                "Run queue unavailable; database reconciliation remains active",
                extra={"event": event, "worker_id": self.worker_id},
            )
        self._queue_available = False
        self._queue_initialized = False
        self._autoclaim_cursor = "0-0"

    def _queue_recovered(self) -> None:
        if self._queue_available is False:
            logger.info(
                "Run queue connection recovered",
                extra={"event": "run_queue_recovered", "worker_id": self.worker_id},
            )
        self._queue_available = True

    def _reap_expired(self) -> ReapReport | None:
        try:
            report = self._repository.reap_expired()
        except SQLAlchemyError:
            self._database_failure("worker_reap_database_unavailable")
            return None
        self._database_recovered()
        return report

    def _due_run_ids(self) -> tuple[str, ...] | None:
        try:
            run_ids = self._repository.due_run_ids(limit=1)
        except SQLAlchemyError:
            self._database_failure("worker_scan_database_unavailable")
            return None
        self._database_recovered()
        return run_ids

    def _database_failure(self, event: str) -> None:
        if self._database_available is not False:
            logger.warning(
                "Worker database unavailable; task delivery is paused",
                extra={"event": event, "worker_id": self.worker_id},
            )
        self._database_available = False

    def _database_recovered(self) -> None:
        if self._database_available is False:
            logger.info(
                "Worker database connection recovered",
                extra={"event": "worker_database_recovered", "worker_id": self.worker_id},
            )
        self._database_available = True
