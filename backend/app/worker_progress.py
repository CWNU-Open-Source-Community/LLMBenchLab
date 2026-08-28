"""Coalesced DB-time Worker main-loop progress and its aggregate read model."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntFlag, StrEnum
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.clock import database_utc_now
from app.models import WorkerProcess

logger = logging.getLogger(__name__)


class WorkerProgressEvent(StrEnum):
    """Allowlisted durable activities accepted by a Worker progress observer."""

    SCAN = "scan"
    CLAIM = "claim"
    PROGRESS = "progress"
    LEASE_HEARTBEAT = "lease_heartbeat"


class WorkerProgressObserver(Protocol):
    """No-throw note interface used at post-commit execution boundaries."""

    def note(self, event: WorkerProgressEvent) -> None: ...


class WorkerProgressLifecycle(WorkerProgressObserver, Protocol):
    """Lifecycle used only by the long-running Worker service."""

    async def start(self) -> None: ...

    async def stop(self) -> bool: ...


class NullWorkerProgressObserver:
    """Compatibility observer for runners and tools without a long-lived Worker."""

    def note(self, event: WorkerProgressEvent) -> None:
        del event


NULL_WORKER_PROGRESS_OBSERVER = NullWorkerProgressObserver()


class WorkerProgressStateError(RuntimeError):
    """Raised when a generation is missing, stopped, or otherwise not writable."""


class _PendingEvent(IntFlag):
    NONE = 0
    SCAN = 1
    CLAIM = 2
    PROGRESS = 4
    LEASE_HEARTBEAT = 8


_EVENT_BITS = {
    WorkerProgressEvent.SCAN: _PendingEvent.SCAN,
    WorkerProgressEvent.CLAIM: _PendingEvent.CLAIM,
    WorkerProgressEvent.PROGRESS: _PendingEvent.PROGRESS,
    WorkerProgressEvent.LEASE_HEARTBEAT: _PendingEvent.LEASE_HEARTBEAT,
}


@dataclass(frozen=True, slots=True)
class WorkerProgressSnapshot:
    """Low-cardinality aggregate at one caller-supplied database timestamp."""

    expected: int
    registered: int
    live: int
    stalled: int
    shortfall: int
    stale_seconds: float
    last_seen_at: datetime | None
    last_scan_at: datetime | None
    last_claim_at: datetime | None
    last_progress_at: datetime | None
    last_lease_heartbeat_at: datetime | None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def collect_worker_progress(
    session: Session,
    *,
    now: datetime,
    stale_seconds: float,
    expected: int,
) -> WorkerProgressSnapshot:
    """Aggregate active generations without exposing process or Worker identifiers."""

    if stale_seconds <= 0 or expected < 0:
        raise ValueError("Worker progress aggregate inputs must be non-negative")
    normalized_now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    cutoff = normalized_now - timedelta(seconds=stale_seconds)
    active = WorkerProcess.stopped_at.is_(None)
    row = session.execute(
        select(
            func.count(WorkerProcess.generation_id).filter(active),
            func.count(WorkerProcess.generation_id).filter(
                active, WorkerProcess.last_seen_at >= cutoff
            ),
            func.count(WorkerProcess.generation_id).filter(
                active, WorkerProcess.last_seen_at < cutoff
            ),
            func.max(case((active, WorkerProcess.last_seen_at), else_=None)),
            func.max(case((active, WorkerProcess.last_scan_at), else_=None)),
            func.max(case((active, WorkerProcess.last_claim_at), else_=None)),
            func.max(case((active, WorkerProcess.last_progress_at), else_=None)),
            func.max(case((active, WorkerProcess.last_lease_heartbeat_at), else_=None)),
        )
    ).one()
    registered = int(row[0] or 0)
    live = int(row[1] or 0)
    stalled = int(row[2] or 0)
    return WorkerProgressSnapshot(
        expected=expected,
        registered=registered,
        live=live,
        stalled=stalled,
        shortfall=max(expected - live, 0),
        stale_seconds=stale_seconds,
        last_seen_at=_as_utc(row[3]),
        last_scan_at=_as_utc(row[4]),
        last_claim_at=_as_utc(row[5]),
        last_progress_at=_as_utc(row[6]),
        last_lease_heartbeat_at=_as_utc(row[7]),
    )


class WorkerProgressRecorder:
    """Register one generation and coalesce real progress into short transactions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        worker_id: str,
        flush_seconds: float,
        generation_id: str | None = None,
    ) -> None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1 to 128 characters")
        if flush_seconds <= 0:
            raise ValueError("flush_seconds must be positive")
        canonical_generation = generation_id or str(uuid4())
        try:
            parsed_generation = UUID(canonical_generation)
        except (TypeError, ValueError) as exc:
            raise ValueError("generation_id must be a canonical UUID") from exc
        if str(parsed_generation) != canonical_generation:
            raise ValueError("generation_id must be a canonical UUID")
        self._session_factory = session_factory
        self._worker_id = worker_id
        self.generation_id = canonical_generation
        self._flush_seconds = flush_seconds
        self._state_lock = Lock()
        self._pending = _PendingEvent.NONE
        self._registered = False
        self._stopping = False
        self._stopped = False
        self._flush_stop: asyncio.Event | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._flush_lock: asyncio.Lock | None = None

    def note(self, event: WorkerProgressEvent) -> None:
        """Record an allowlisted event in memory; this method deliberately never raises."""

        bit = _EVENT_BITS.get(event)
        if bit is None:
            return
        with self._state_lock:
            if self._registered and not self._stopping and not self._stopped:
                self._pending |= bit

    async def start(self) -> None:
        """Register before execution and start the sole periodic flush loop."""

        if self._registered or self._flush_task is not None:
            raise WorkerProgressStateError("worker_progress_already_started")
        await asyncio.to_thread(self._register)
        with self._state_lock:
            self._registered = True
        self._flush_stop = asyncio.Event()
        self._flush_lock = asyncio.Lock()
        self._flush_task = asyncio.create_task(self._flush_loop(), name="worker-progress-flush")

    async def flush_now(self) -> bool:
        """Try one coalesced flush; false means no event or a conservative retry."""

        flush_lock = self._flush_lock
        if flush_lock is None:
            return False
        async with flush_lock:
            return await self._flush_once()

    async def _flush_once(self) -> bool:
        with self._state_lock:
            if (
                not self._registered
                or self._stopping
                or self._stopped
                or self._pending == _PendingEvent.NONE
            ):
                return False
            pending = self._pending
            self._pending = _PendingEvent.NONE
        try:
            await asyncio.to_thread(self._flush, pending)
        except Exception as exc:
            with self._state_lock:
                self._pending |= pending
            logger.warning(
                "Worker progress flush failed; retained for retry",
                extra={
                    "event": "worker_progress_flush_failed",
                    "error_code": f"worker_progress_error:{type(exc).__name__}",
                    "result": "retained",
                },
            )
            return False
        return True

    async def stop(self) -> bool:
        """Stop periodic writes and atomically persist pending bits plus graceful stop."""

        flush_stop = self._flush_stop
        flush_task = self._flush_task
        with self._state_lock:
            if not self._registered or self._stopped:
                return False
            self._stopping = True
        if flush_stop is not None:
            flush_stop.set()
        if flush_task is not None:
            await flush_task
        flush_lock = self._flush_lock
        if flush_lock is None:
            return False
        async with flush_lock:
            with self._state_lock:
                if not self._registered or self._stopped:
                    return False
                pending = self._pending
                self._pending = _PendingEvent.NONE
            try:
                await asyncio.to_thread(self._stop, pending)
            except Exception as exc:
                with self._state_lock:
                    self._pending |= pending
                logger.warning(
                    "Worker progress graceful stop failed; generation will become stale",
                    extra={
                        "event": "worker_progress_stop_failed",
                        "error_code": f"worker_progress_error:{type(exc).__name__}",
                        "result": "stale",
                    },
                )
                return False
            with self._state_lock:
                self._stopped = True
            return True

    async def _flush_loop(self) -> None:
        stop = self._flush_stop
        if stop is None:
            return
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._flush_seconds)
            except TimeoutError:
                await self.flush_now()

    def _register(self) -> None:
        with self._session_factory() as session, session.begin():
            now = database_utc_now(session)
            session.add(
                WorkerProcess(
                    generation_id=self.generation_id,
                    worker_id=self._worker_id,
                    started_at=now,
                    last_seen_at=now,
                )
            )

    def _flush(self, pending: _PendingEvent) -> None:
        with self._session_factory() as session, session.begin():
            now = database_utc_now(session)
            values = self._event_values(pending, now=now)
            result = session.execute(
                update(WorkerProcess)
                .where(
                    WorkerProcess.generation_id == self.generation_id,
                    WorkerProcess.worker_id == self._worker_id,
                    WorkerProcess.stopped_at.is_(None),
                )
                .values(last_seen_at=now, **values)
            )
            if result.rowcount != 1:
                raise WorkerProgressStateError("worker_progress_generation_not_active")

    def _stop(self, pending: _PendingEvent) -> None:
        with self._session_factory() as session, session.begin():
            now = database_utc_now(session)
            values: dict[str, datetime] = {"stopped_at": now}
            if pending != _PendingEvent.NONE:
                values["last_seen_at"] = now
                values.update(self._event_values(pending, now=now))
            result = session.execute(
                update(WorkerProcess)
                .where(
                    WorkerProcess.generation_id == self.generation_id,
                    WorkerProcess.worker_id == self._worker_id,
                    WorkerProcess.stopped_at.is_(None),
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise WorkerProgressStateError("worker_progress_generation_not_active")

    @staticmethod
    def _event_values(
        pending: _PendingEvent,
        *,
        now: datetime,
    ) -> dict[str, datetime]:
        values: dict[str, datetime] = {}
        if pending & _PendingEvent.SCAN:
            values["last_scan_at"] = now
        if pending & _PendingEvent.CLAIM:
            values["last_claim_at"] = now
        if pending & _PendingEvent.PROGRESS:
            values["last_progress_at"] = now
        if pending & _PendingEvent.LEASE_HEARTBEAT:
            values["last_lease_heartbeat_at"] = now
        return values
