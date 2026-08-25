"""Worker dependency-capability probe behavior and secret-safety tests."""

from __future__ import annotations

import json

import pytest

import app.worker_probe as probe_module
from app.task_queue import QueueUnavailable


class _Engine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class _UnavailableQueue:
    def __init__(self) -> None:
        self.closed = False

    async def ping(self) -> bool:
        raise QueueUnavailable("redis://user:secret@queue/private")

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_worker_probe_sanitizes_database_startup_failure(
    monkeypatch,
    capsys,
) -> None:
    engine = _Engine()
    secret = "postgresql://user:secret@database/private"
    monkeypatch.setattr(probe_module, "engine", engine)

    def fail_database():
        raise RuntimeError(secret)

    monkeypatch.setattr(probe_module, "initialize_database", fail_database)

    assert await probe_module._probe() == 1

    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "not_ready", "database": "unavailable"}
    assert secret not in output
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_worker_probe_reports_queue_degradation_but_keeps_reconciliation_ready(
    monkeypatch,
    capsys,
) -> None:
    engine = _Engine()
    queue = _UnavailableQueue()
    monkeypatch.setattr(probe_module, "engine", engine)
    monkeypatch.setattr(probe_module, "initialize_database", lambda: 0)
    monkeypatch.setattr(probe_module, "create_run_queue", lambda _settings: queue)

    assert await probe_module._probe() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "degraded",
        "database": "ok",
        "schema": "ok",
        "queue": "unavailable",
        "database_reconciliation": "available",
    }
    assert queue.closed is True
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_worker_probe_sanitizes_invalid_queue_configuration(
    monkeypatch,
    capsys,
) -> None:
    engine = _Engine()
    secret = "redis://user:secret@queue/private"
    monkeypatch.setattr(probe_module, "engine", engine)
    monkeypatch.setattr(probe_module, "initialize_database", lambda: 0)

    def fail_queue(_settings):
        raise ValueError(secret)

    monkeypatch.setattr(probe_module, "create_run_queue", fail_queue)

    assert await probe_module._probe() == 1

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "status": "not_ready",
        "database": "ok",
        "schema": "ok",
        "queue": "configuration_error",
    }
    assert secret not in output
    assert engine.disposed is True
