"""Trusted-local explicit governance policy inspection and activation."""

import logging

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import SessionDep
from app.governance import (
    GovernanceIntegrityError,
    GovernanceRepository,
    record_governance_integrity_event,
)
from app.schemas.governance import GovernancePolicyApply, GovernancePolicyRead

router = APIRouter(prefix="/governance", tags=["governance"])
logger = logging.getLogger(__name__)


def _session_factory(session: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=session.get_bind(),
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )


def _repository(session: Session) -> GovernanceRepository:
    return GovernanceRepository(_session_factory(session))


def _raise_integrity_error(session: Session) -> None:
    session.rollback()
    try:
        record_governance_integrity_event(_session_factory(session))
    except Exception:
        logger.error(
            "Governance integrity evidence could not be recorded",
            extra={"event": "governance_integrity_audit_failed", "result": "not_recorded"},
        )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "code": "governance_integrity_error",
            "message": "Governance state failed integrity validation.",
        },
        headers={"Cache-Control": "no-store"},
    ) from None


@router.get("/policy", response_model=GovernancePolicyRead, summary="查看当前治理策略")
def get_governance_policy(session: SessionDep, response: Response):
    try:
        policy = _repository(session).active_policy(session)
    except GovernanceIntegrityError:
        _raise_integrity_error(session)
    response.headers["Cache-Control"] = "no-store"
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "governance_policy_not_initialized",
                "message": "No governance policy has been initialized yet.",
            },
            headers={"Cache-Control": "no-store"},
        )
    return policy


@router.put("/policy", response_model=GovernancePolicyRead, summary="原子应用完整治理策略")
def apply_governance_policy(
    payload: GovernancePolicyApply,
    session: SessionDep,
    response: Response,
):
    try:
        policy = _repository(session).apply_policy(session, payload.model_dump())
    except GovernanceIntegrityError:
        _raise_integrity_error(session)
    # Materialize the response while this transaction still owns the policy
    # serialization lock.  A later concurrent PUT may legitimately deactivate
    # this immutable row immediately after commit, but must not rewrite the
    # response describing this request's linearization point.
    policy_snapshot = GovernancePolicyRead.model_validate(policy)
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    return policy_snapshot
