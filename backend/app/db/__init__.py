"""Database metadata plus lazy runtime session exports.

Importing a schema module must not construct the process-wide engine.  In
particular, the offline audit-archive verifier imports ORM metadata only to
validate the frozen row contract; it must remain independent of database
settings and filesystem paths.  Existing ``from app.db import ...`` callers
retain the same public names through module-level lazy attributes.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.session import SessionLocal, engine, get_db

__all__ = ["Base", "SessionLocal", "engine", "get_db"]

_SESSION_EXPORTS = frozenset({"SessionLocal", "engine", "get_db"})


def __getattr__(name: str) -> Any:
    if name not in _SESSION_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("app.db.session"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
