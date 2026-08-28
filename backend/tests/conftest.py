"""Isolated SQLite fixtures; environment is set before importing the application."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="llmbenchlab-pytest-")
_DATABASE_PATH = Path(_TEST_ROOT.name) / "tests.db"
_CREDENTIAL_KEYS_PATH = Path(_TEST_ROOT.name) / "credential-keys.json"
_CREDENTIAL_KEYS_PATH.write_text(
    json.dumps(
        {
            "active_key_id": "fixture-v1",
            "keys": {"fixture-v1": base64.urlsafe_b64encode(bytes([1]) * 32).decode("ascii")},
        }
    ),
    encoding="utf-8",
)
os.environ["LLMBENCHLAB_DATABASE_URL"] = f"sqlite:///{_DATABASE_PATH}"
os.environ["LLMBENCHLAB_CREDENTIAL_KEYS_FILE"] = str(_CREDENTIAL_KEYS_PATH)
os.environ["LLMBENCHLAB_TRUSTED_HOSTS"] = "localhost,127.0.0.1,testserver"
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
