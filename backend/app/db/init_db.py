"""Database readiness checks and safe restart recovery."""

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.constants import INTERRUPTED_RUN_MESSAGE
from app.core.time import utc_now
from app.db.prepare_migrations import require_database_at_head
from app.db.session import SessionLocal, engine
from app.models import EvaluationRun, RunStatus


def mark_interrupted_runs_failed(session: Session) -> int:
    """Move stale ``running`` rows to ``failed`` after a process restart."""

    result = session.execute(
        update(EvaluationRun)
        .where(EvaluationRun.status == RunStatus.RUNNING)
        .values(
            status=RunStatus.FAILED,
            finished_at=utc_now(),
            error_message=INTERRUPTED_RUN_MESSAGE,
        )
    )
    session.commit()
    return int(result.rowcount or 0)


def initialize_database() -> int:
    """Require the Alembic head and recover runs left active by a prior process."""

    require_database_at_head(engine)
    with SessionLocal() as session:
        return mark_interrupted_runs_failed(session)
