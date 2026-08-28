"""Cross-database locking for Model/Run configuration coordination."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Model


def lock_model_for_update(session: Session, model_id: str) -> Model | None:
    """Lock and return one Model before Run creation or sensitive mutation.

    PostgreSQL honors the row-level ``FOR UPDATE`` clause. SQLite ignores that
    clause, so its supported single-process path must acquire the database write
    lock before reading the Model. Call this helper as the first database
    operation in the transaction; all callers coordinating Run creation with
    endpoint or credential mutation must use the same helper.

    SQLite's lock is necessarily database-wide. That is stricter than the
    PostgreSQL row lock, but it prevents a transaction from reading an old Model
    snapshot and only later discovering that another writer changed its Provider
    target or credential.
    """

    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    return session.scalar(select(Model).where(Model.id == model_id).with_for_update())
