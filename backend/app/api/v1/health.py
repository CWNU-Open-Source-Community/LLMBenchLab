"""Local health and capability endpoints."""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import SessionDep, SettingsDep
from app.core.constants import PROTOCOL_VERSION
from app.core.time import utc_now
from app.schemas.system import HealthResponse, InfoResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="检查 API 与数据库健康状态")
def health(session: SessionDep, settings: SettingsDep) -> HealthResponse:
    """Check only local persistence; no model provider is contacted."""

    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "database_unavailable", "message": "Database health check failed"},
        ) from exc
    return HealthResponse(
        status="ok",
        database="ok",
        version=settings.app_version,
        timestamp=utc_now(),
    )


@router.get("/info", response_model=InfoResponse, summary="查看服务与协议信息")
def info(settings: SettingsDep) -> InfoResponse:
    return InfoResponse(
        name=settings.app_name,
        version=settings.app_version,
        api_version="v1",
        protocol_version=PROTOCOL_VERSION,
        environment=settings.environment,
        capabilities={
            "providers": ["mock", "openai_compatible"],
            "question_types": ["exact_match", "multiple_choice", "numeric"],
            "runner": "independent_database_lease_worker",
        },
    )
