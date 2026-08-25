"""Secret-safe CRUD endpoints for registered model configurations."""

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import PaginationDep, SessionDep
from app.core.time import utc_now
from app.models import EvaluationRun, ProviderType
from app.models import Model as RegisteredModel
from app.schemas.model import ModelCreate, ModelList, ModelRead, ModelUpdate

router = APIRouter(prefix="/models", tags=["models"])


def _get_model_or_404(session: SessionDep, model_id: str) -> RegisteredModel:
    model = session.get(RegisteredModel, model_id)
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "model_not_found", "message": "Model was not found"},
        )
    return model


def _raise_name_conflict(exc: IntegrityError) -> None:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "model_name_conflict", "message": "A model with this name already exists"},
    ) from exc


@router.get("", response_model=ModelList, summary="分页列出模型")
def list_models(
    session: SessionDep,
    pagination: PaginationDep,
    provider_type: ProviderType | None = None,
    enabled: bool | None = None,
) -> ModelList:
    filters = []
    if provider_type is not None:
        filters.append(RegisteredModel.provider_type == provider_type)
    if enabled is not None:
        filters.append(RegisteredModel.enabled == enabled)

    total = session.scalar(select(func.count()).select_from(RegisteredModel).where(*filters)) or 0
    statement = (
        select(RegisteredModel)
        .where(*filters)
        .order_by(RegisteredModel.created_at.desc(), RegisteredModel.id)
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    items = list(session.scalars(statement))
    return ModelList(
        items=items,
        total=total,
        offset=pagination.offset,
        limit=pagination.limit,
    )


@router.post("", response_model=ModelRead, status_code=status.HTTP_201_CREATED, summary="注册模型")
def create_model(payload: ModelCreate, session: SessionDep) -> RegisteredModel:
    model = RegisteredModel(**payload.model_dump())
    session.add(model)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        _raise_name_conflict(exc)
    session.refresh(model)
    return model


@router.get("/{model_id}", response_model=ModelRead, summary="查看模型")
def get_model(model_id: str, session: SessionDep) -> RegisteredModel:
    return _get_model_or_404(session, model_id)


@router.patch("/{model_id}", response_model=ModelRead, summary="更新模型")
def update_model(
    model_id: str,
    payload: ModelUpdate,
    session: SessionDep,
) -> RegisteredModel:
    model = _get_model_or_404(session, model_id)
    current = {
        "name": model.name,
        "provider_type": model.provider_type,
        "base_url": model.base_url,
        "remote_model_name": model.remote_model_name,
        "api_key_env": model.api_key_env,
        "enabled": model.enabled,
        "input_price_per_million": model.input_price_per_million,
        "output_price_per_million": model.output_price_per_million,
        "default_parameters": model.default_parameters,
    }
    merged = current | payload.model_dump(exclude_unset=True)
    try:
        validated = ModelCreate.model_validate(merged)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(), body=payload.model_dump()) from exc

    for field, value in validated.model_dump().items():
        setattr(model, field, value)
    model.updated_at = utc_now()
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        _raise_name_conflict(exc)
    session.refresh(model)
    return model


@router.delete(
    "/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删除未被历史 Run 引用的模型",
)
def delete_model(model_id: str, session: SessionDep) -> Response:
    model = _get_model_or_404(session, model_id)
    run_id = session.scalar(
        select(EvaluationRun.id).where(EvaluationRun.model_id == model_id).limit(1)
    )
    if run_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "model_has_runs",
                "message": "Model is referenced by historical runs and cannot be deleted",
            },
        )
    session.delete(model)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "model_delete_conflict", "message": "Model cannot be deleted"},
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
