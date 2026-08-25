"""Isolated SQLite fixtures; environment is set before importing the application."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="llmbenchlab-pytest-")
_DATABASE_PATH = Path(_TEST_ROOT.name) / "tests.db"
os.environ["LLMBENCHLAB_DATABASE_URL"] = f"sqlite:///{_DATABASE_PATH}"
os.environ["LLMBENCHLAB_LOG_LEVEL"] = "WARNING"
os.environ["LLMBENCHLAB_REDIS_URL"] = ""

from app.db.base import Base  # noqa: E402
from app.db.prepare_migrations import stamp_database  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Return an API client backed by a freshly created temporary database."""

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    stamp_database(engine, "head")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_session():
    """Return a short-lived session bound to the test database."""

    with SessionLocal() as session:
        yield session
