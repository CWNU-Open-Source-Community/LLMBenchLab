"""Secret-safe CRUD endpoints for registered model configurations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.deps import PaginationDep, SessionDep, SettingsDep
from app.core.config import Settings
from app.core.time import utc_now
from app.db.model_lock import lock_model_for_update
from app.models import (
    CredentialSource,
    EvaluationRun,
    ModelCredential,
    ProviderType,
    RunStatus,
)
from app.models import Model as RegisteredModel
from app.schemas.model import (
    ModelCreate,
    ModelList,
    ModelRead,
    ModelUpdate,
    model_public_values_contain_secret,
)
from app.security import (
    CredentialCryptoError,
    CredentialEnvelopeError,
    CredentialKeyring,
    EncryptedCredential,
    normalize_provider_origin,
)
from app.services.credential_audit import (
    audit_credential_changed,
    audit_credential_decrypt_failed,
    audit_credential_rejected_after_rollback,
    credential_change_action,
    safe_credential_key_id,
)

router = APIRouter(prefix="/models", tags=["models"])


def _credential_key_id(model: RegisteredModel) -> str | None:
    credential = model.credential
    return safe_credential_key_id(credential.key_id if credential is not None else None)


def _get_model_or_404(
    session: SessionDep,
    model_id: str,
    *,
    for_update: bool = False,
) -> RegisteredModel:
    if for_update:
        model = lock_model_for_update(session, model_id)
    else:
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


def _raise_credential_store_unavailable(exc: CredentialCryptoError) -> None:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "credential_store_unavailable",
            "message": "Encrypted credential storage is not available",
        },
    ) from exc


def _load_keyring(settings: Settings) -> CredentialKeyring:
    try:
        return CredentialKeyring.from_file(settings.credential_keys_file)
    except CredentialCryptoError as exc:
        _raise_credential_store_unavailable(exc)


def _encrypt_api_key(
    payload: ModelCreate | ModelUpdate,
    *,
    model_id: str,
    base_url: str,
    settings: Settings,
) -> EncryptedCredential:
    if payload.api_key is None:
        raise AssertionError("api_key is required")
    try:
        return _load_keyring(settings).encrypt(
            payload.api_key,
            model_id=model_id,
            provider_base_url=base_url,
        )
    except CredentialCryptoError as exc:
        _raise_credential_store_unavailable(exc)


def _decrypt_model_api_key(
    model: RegisteredModel,
    *,
    settings: Settings,
) -> SecretStr:
    credential = model.credential
    if model.base_url is None or credential is None:
        raise CredentialEnvelopeError
    encrypted = EncryptedCredential(
        key_id=credential.key_id,
        algorithm=credential.algorithm,
        nonce=credential.nonce,
        ciphertext=credential.ciphertext,
    )
    return CredentialKeyring.from_file(settings.credential_keys_file).decrypt(
        encrypted,
        model_id=model.id,
        provider_base_url=model.base_url,
    )


def _safe_validation_error(exc: ValidationError) -> RequestValidationError:
    errors = [
        {key: error[key] for key in ("type", "loc", "msg") if key in error}
        for error in exc.errors()
    ]
    return RequestValidationError(errors)


def _source_for(validated: ModelCreate) -> CredentialSource:
    if validated.provider_type == ProviderType.MOCK:
        return CredentialSource.NONE
    if validated.api_key is not None:
        return CredentialSource.STORED
    return CredentialSource.ENVIRONMENT


def _credential_row(model_id: str, encrypted: EncryptedCredential) -> ModelCredential:
    return ModelCredential(
        model_id=model_id,
        algorithm=encrypted.algorithm,
        key_id=encrypted.key_id,
        nonce=encrypted.nonce,
        ciphertext=encrypted.ciphertext,
    )


def _active_run_exists(session: SessionDep, model_id: str) -> bool:
    run_id = session.scalar(
        select(EvaluationRun.id)
        .where(
            EvaluationRun.model_id == model_id,
            EvaluationRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
        )
        .limit(1)
    )
    return run_id is not None


def _reject_public_secret_copy(
    model: ModelCreate,
    *,
    credential_source: CredentialSource,
    secret: str,
    model_id: str,
    created_at: datetime,
    updated_at: datetime,
) -> None:
    if not model_public_values_contain_secret(
        model,
        credential_source=credential_source,
        secret=secret,
        model_id=model_id,
        created_at=created_at,
        updated_at=updated_at,
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "code": "api_key_duplicated_in_public_fields",
            "message": "API key must not be duplicated in public model fields",
        },
    )


def _same_persisted_price(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError):
        return False


def _unreadable_credential_recovery_is_isolated(
    model: RegisteredModel,
    validated: ModelCreate,
    source: CredentialSource,
) -> bool:
    """Allow recovery only when it cannot introduce unrelated public values."""

    common_unchanged = (
        validated.name == model.name
        and validated.enabled == model.enabled
        and validated.default_parameters == model.default_parameters
    )
    if not common_unchanged:
        return False

    if source == CredentialSource.STORED:
        return (
            validated.provider_type == model.provider_type
            and validated.base_url == model.base_url
            and validated.remote_model_name == model.remote_model_name
            and validated.api_key_env == model.api_key_env
            and _same_persisted_price(
                validated.input_price_per_million,
                model.input_price_per_million,
            )
            and _same_persisted_price(
                validated.output_price_per_million,
                model.output_price_per_million,
            )
        )
    if source == CredentialSource.ENVIRONMENT:
        return (
            validated.provider_type == model.provider_type
            and validated.base_url == model.base_url
            and validated.remote_model_name == model.remote_model_name
            and _same_persisted_price(
                validated.input_price_per_million,
                model.input_price_per_million,
            )
            and _same_persisted_price(
                validated.output_price_per_million,
                model.output_price_per_million,
            )
        )
    return (
        validated.provider_type == ProviderType.MOCK
        and validated.base_url is None
        and validated.remote_model_name is None
        and validated.api_key_env is None
        and validated.input_price_per_million == 0
        and validated.output_price_per_million == 0
    )


def _reject_nonisolated_credential_recovery() -> None:
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "code": "credential_recovery_requires_isolated_update",
            "message": (
                "Replace or remove an unreadable credential before changing other model fields"
            ),
        },
    )


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
        .options(selectinload(RegisteredModel.credential))
        .where(*filters)
        .order_by(RegisteredModel.created_at.desc(), RegisteredModel.id)
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    items = list(session.scalars(statement))
    return ModelList(items=items, total=total, offset=pagination.offset, limit=pagination.limit)


@router.post("", response_model=ModelRead, status_code=status.HTTP_201_CREATED, summary="注册模型")
def create_model(
    payload: ModelCreate,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> RegisteredModel:
    model_id = str(uuid4())
    source = _source_for(payload)
    values = payload.model_dump(exclude={"api_key"})
    values["credential_source"] = source
    timestamp = utc_now()
    model = RegisteredModel(
        id=model_id,
        created_at=timestamp,
        updated_at=timestamp,
        **values,
    )
    credential_key_id: str | None = None
    if source == CredentialSource.STORED:
        if model.base_url is None:
            raise AssertionError("stored credentials require base_url")
        if payload.api_key is None:
            raise AssertionError("stored credentials require api_key")
        _reject_public_secret_copy(
            payload,
            credential_source=source,
            secret=payload.api_key.get_secret_value(),
            model_id=model.id,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )
        encrypted = _encrypt_api_key(
            payload,
            model_id=model.id,
            base_url=model.base_url,
            settings=settings,
        )
        model.credential = _credential_row(model.id, encrypted)
        credential_key_id = encrypted.key_id
    session.add(model)
    if source != CredentialSource.NONE:
        audit_credential_changed(
            session,
            model_id=model.id,
            action="created",
            source=source,
            key_id=credential_key_id,
        )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        _raise_name_conflict(exc)
    session.refresh(model)
    response.headers["Cache-Control"] = "no-store"
    return model


@router.get("/{model_id}", response_model=ModelRead, summary="查看模型")
def get_model(model_id: str, session: SessionDep) -> RegisteredModel:
    return _get_model_or_404(session, model_id)


@router.patch("/{model_id}", response_model=ModelRead, summary="更新模型")
def update_model(
    model_id: str,
    payload: ModelUpdate,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> RegisteredModel:
    model = _get_model_or_404(session, model_id, for_update=True)
    previous_source = model.credential_source
    previous_api_key_env = model.api_key_env
    previous_key_id = _credential_key_id(model)
    current = {
        "name": model.name,
        "provider_type": model.provider_type,
        "base_url": model.base_url,
        "remote_model_name": model.remote_model_name,
        "api_key_env": model.api_key_env,
        # This non-secret marker reconstructs a stored source without decrypting it.
        "api_key": (
            "configured-stored-credential"
            if model.credential_source == CredentialSource.STORED
            else None
        ),
        "enabled": model.enabled,
        "input_price_per_million": model.input_price_per_million,
        "output_price_per_million": model.output_price_per_million,
        "default_parameters": model.default_parameters,
    }
    changes = payload.model_dump(exclude_unset=True)
    if payload.api_key is not None:
        changes["api_key_env"] = None
    elif changes.get("api_key_env") is not None:
        current["api_key"] = None
    if changes.get("provider_type") == ProviderType.MOCK:
        current["api_key"] = None
        changes.update(
            base_url=None,
            remote_model_name=None,
            api_key_env=None,
        )
    merged = current | changes
    try:
        validated = ModelCreate.model_validate(merged)
    except ValidationError as exc:
        raise _safe_validation_error(exc) from exc

    source = _source_for(validated)
    update_timestamp = utc_now()
    if payload.api_key is not None:
        _reject_public_secret_copy(
            validated,
            credential_source=source,
            secret=payload.api_key.get_secret_value(),
            model_id=model.id,
            created_at=model.created_at,
            updated_at=update_timestamp,
        )
    sensitive_change = any(
        (
            validated.provider_type != model.provider_type,
            validated.base_url != model.base_url,
            validated.remote_model_name != model.remote_model_name,
            validated.api_key_env != model.api_key_env,
            source != model.credential_source,
            payload.api_key is not None,
        )
    )
    if sensitive_change and _active_run_exists(session, model.id):
        # Reject from public, non-secret facts before touching the existing
        # envelope.  Besides avoiding unnecessary decryption, this keeps the
        # durable rejection audit from rolling back an earlier decrypt event.
        audit_credential_rejected_after_rollback(
            session,
            model_id=model.id,
            reason="active_run_conflict",
            source=source,
            key_id=previous_key_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "model_has_active_runs",
                "message": "Provider endpoint or credentials cannot change during active runs",
            },
        )
    if model.credential_source == CredentialSource.STORED:
        try:
            existing_api_key = _decrypt_model_api_key(
                model,
                settings=settings,
            ).get_secret_value()
        except CredentialCryptoError as exc:
            # A write-only replacement or an explicit switch away from stored
            # credentials can safely repair/remove an unreadable old envelope.
            preserving_unreadable = payload.api_key is None and source == CredentialSource.STORED
            recovery_isolated = _unreadable_credential_recovery_is_isolated(
                model,
                validated,
                source,
            )
            if preserving_unreadable or not recovery_isolated:
                audit_credential_decrypt_failed(
                    session,
                    model_id=model.id,
                    key_id=previous_key_id,
                    after_rollback=True,
                )
            else:
                audit_credential_decrypt_failed(
                    session,
                    model_id=model.id,
                    key_id=previous_key_id,
                    after_rollback=False,
                )
            if preserving_unreadable:
                _raise_credential_store_unavailable(exc)
            if not recovery_isolated:
                _reject_nonisolated_credential_recovery()
        else:
            _reject_public_secret_copy(
                validated,
                credential_source=source,
                secret=existing_api_key,
                model_id=model.id,
                created_at=model.created_at,
                updated_at=update_timestamp,
            )
    old_origin = (
        normalize_provider_origin(model.base_url)
        if model.provider_type == ProviderType.OPENAI_COMPATIBLE and model.base_url is not None
        else None
    )
    new_origin = (
        normalize_provider_origin(validated.base_url)
        if validated.provider_type == ProviderType.OPENAI_COMPATIBLE
        and validated.base_url is not None
        else None
    )
    if (
        old_origin is not None
        and new_origin is not None
        and new_origin != old_origin
        and payload.api_key is None
    ):
        audit_credential_rejected_after_rollback(
            session,
            model_id=model.id,
            reason="origin_rejected",
            source=source,
            key_id=previous_key_id,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "api_key_required_for_origin_change",
                "message": "Changing the Provider origin requires a new API key",
            },
        )

    encrypted: EncryptedCredential | None = None
    if payload.api_key is not None:
        if validated.base_url is None:
            raise AssertionError("stored credentials require base_url")
        encrypted = _encrypt_api_key(
            payload,
            model_id=model.id,
            base_url=validated.base_url,
            settings=settings,
        )

    for field, value in validated.model_dump(exclude={"api_key"}).items():
        setattr(model, field, value)
    model.credential_source = source
    if encrypted is not None:
        if model.credential is None:
            model.credential = _credential_row(model.id, encrypted)
        else:
            model.credential.algorithm = encrypted.algorithm
            model.credential.key_id = encrypted.key_id
            model.credential.nonce = encrypted.nonce
            model.credential.ciphertext = encrypted.ciphertext
            model.credential.updated_at = utc_now()
    elif source != CredentialSource.STORED:
        model.credential = None
    model.updated_at = update_timestamp
    credential_action = credential_change_action(
        previous_source=previous_source,
        new_source=source,
        api_key_replaced=payload.api_key is not None,
        environment_name_changed=validated.api_key_env != previous_api_key_env,
    )
    if credential_action is not None:
        changed_key_id = encrypted.key_id if encrypted is not None else previous_key_id
        audit_credential_changed(
            session,
            model_id=model.id,
            action=credential_action,
            source=source,
            key_id=changed_key_id,
        )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        _raise_name_conflict(exc)
    session.refresh(model)
    response.headers["Cache-Control"] = "no-store"
    return model


@router.delete(
    "/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删除未被历史 Run 引用的模型",
)
def delete_model(model_id: str, session: SessionDep) -> Response:
    model = _get_model_or_404(session, model_id)
    previous_source = model.credential_source
    previous_key_id = _credential_key_id(model)
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
    if previous_source != CredentialSource.NONE:
        audit_credential_changed(
            session,
            model_id=model_id,
            action="removed",
            source=CredentialSource.NONE,
            key_id=previous_key_id,
        )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "model_delete_conflict", "message": "Model cannot be deleted"},
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
