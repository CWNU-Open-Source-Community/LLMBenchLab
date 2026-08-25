"""Dependency probe for the independent Worker container."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from app.core.config import get_settings
from app.db.init_db import initialize_database
from app.db.session import engine
from app.task_queue import QueueUnavailable, create_run_queue


async def _probe() -> int:
    settings = get_settings()
    queue = None
    try:
        initialize_database()
    except Exception:
        print(json.dumps({"status": "not_ready", "database": "unavailable"}))
        engine.dispose()
        return 1

    try:
        try:
            queue = create_run_queue(settings)
        except Exception:
            print(
                json.dumps(
                    {
                        "status": "not_ready",
                        "database": "ok",
                        "schema": "ok",
                        "queue": "configuration_error",
                    }
                )
            )
            return 1

        queue_status = "disabled"
        if queue is not None:
            try:
                await queue.ping()
            except QueueUnavailable:
                queue_status = "unavailable"
            else:
                queue_status = "ok"
        print(
            json.dumps(
                {
                    "status": "ready" if queue_status != "unavailable" else "degraded",
                    "database": "ok",
                    "schema": "ok",
                    "queue": queue_status,
                    "database_reconciliation": "available",
                }
            )
        )
        return 0
    finally:
        if queue is not None:
            with suppress(Exception):
                await queue.close()
        engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(_probe()))


if __name__ == "__main__":
    main()
