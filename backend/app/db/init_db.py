"""Database revision readiness for API and Worker startup."""

from app.db.prepare_migrations import require_database_at_head
from app.db.session import engine


def initialize_database() -> int:
    """Require Alembic head without mutating task state during process startup."""

    require_database_at_head(engine)
    return 0
