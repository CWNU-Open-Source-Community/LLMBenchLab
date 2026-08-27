"""End-to-end gates for write-only API Keys entered through the Web API."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

import app.api.v1.models as models_api
import app.runners.evaluation_runner as runner_module
from app.adapters import AdapterError, OpenAICompatibleAdapter
from app.core.config import get_settings
from app.core.logging import SanitizedJsonFormatter
from app.db.session import SessionLocal, engine
from app.models import EvaluationRun, Model, ModelCredential
from app.reports import export_run_report
from app.runners.evaluation_runner import EvaluationRunner
from app.security import CredentialCryptoError, CredentialKeyring, EncryptedCredential

CANARY = "sk-web-canary-A7vN4xQ2pL9mT6rK3dW8"
ROTATED_CANARY = "sk-web-rotated-H8qS5nC1yM4kR7vP2tX9"
NUMERIC_CANARY = "42424242"


def _stored_payload(*, name: str = "Web Provider", api_key: str = CANARY) -> dict[str, Any]:
    return {
        "name": name,
        "provider_type": "openai_compatible",
        "base_url": "https://provider.example/v1",
        "remote_model_name": "provider-model",
        "api_key": api_key,
        "enabled": True,
    }


def _create_stored_model(client, *, name: str = "Web Provider", api_key: str = CANARY):
    response = client.post("/api/v1/models", json=_stored_payload(name=name, api_key=api_key))
    assert response.status_code == 201, response.text
    return response


def _create_pending_run(
    client,
    model_id: str,
    **run_overrides: object,
) -> dict[str, Any]:
    benchmark = client.post("/api/v1/benchmarks/reload-demo")
    assert benchmark.status_code in {200, 201}, benchmark.text
    response = client.post(
        "/api/v1/runs",
        json={
            "model_id": model_id,
            "benchmark_id": benchmark.json()["id"],
            **run_overrides,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def _assert_secret_absent(secret: str, *surfaces: object) -> None:
    for surface in surfaces:
        assert secret not in str(surface)


def _encrypted_row(model_id: str) -> ModelCredential:
    with SessionLocal() as session:
        row = session.get(ModelCredential, model_id)
        assert row is not None
        session.expunge(row)
        return row


def _decrypt_row(row: ModelCredential, *, base_url: str) -> str:
    encrypted = EncryptedCredential(
        key_id=row.key_id,
        algorithm=row.algorithm,
        nonce=row.nonce,
        ciphertext=row.ciphertext,
    )
    return (
        CredentialKeyring.from_file(get_settings().credential_keys_file)
        .decrypt(encrypted, model_id=row.model_id, provider_base_url=base_url)
        .get_secret_value()
    )


def test_web_api_key_is_write_only_encrypted_and_absent_from_public_surfaces(
    client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")

    created = _create_stored_model(client)
    payload = created.json()
    model_id = payload["id"]

    assert created.headers["cache-control"] == "no-store"
    assert payload["credential_source"] == "stored"
    assert payload["has_api_key"] is True
    assert payload["api_key_env"] is None
    assert "api_key" not in payload
    _assert_secret_absent(CANARY, created.text, created.headers, caplog.text)

    fetched = client.get(f"/api/v1/models/{model_id}")
    listed = client.get("/api/v1/models")
    assert fetched.status_code == listed.status_code == 200
    _assert_secret_absent(CANARY, fetched.text, listed.text)
    for public_payload in (fetched.json(), listed.json()["items"][0]):
        assert not ({"ciphertext", "nonce", "key_id", "algorithm"} & public_payload.keys())
        assert "api_key" not in public_payload

    run = _create_pending_run(client, model_id)
    run_fetched = client.get(f"/api/v1/runs/{run['id']}")
    run_listed = client.get("/api/v1/runs")
    assert run_fetched.status_code == run_listed.status_code == 200
    assert run["model_parameters_snapshot"]["model"]["credential_source"] == "stored"
    assert run["model_parameters_snapshot"]["model"]["api_key_env"] is None
    _assert_secret_absent(CANARY, run, run_fetched.text, run_listed.text)
    assert not any(
        forbidden in json.dumps(run, sort_keys=True)
        for forbidden in ('"ciphertext"', '"nonce"', '"key_id"')
    )

    row = _encrypted_row(model_id)
    assert row.algorithm == "aes-256-gcm-v1"
    assert len(row.nonce) == 12
    assert CANARY.encode() not in row.ciphertext
    assert _decrypt_row(row, base_url="https://provider.example/v1") == CANARY
    _assert_secret_absent(CANARY, row, repr(row), repr(SecretStr(CANARY)))
    assert engine.hide_parameters is True

    database_path = Path(str(engine.url.database))
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{database_path}{suffix}")
        if candidate.exists():
            assert CANARY.encode() not in candidate.read_bytes()

    openapi = client.get("/openapi.json").json()
    model_create_schema = openapi["components"]["schemas"]["ModelCreate"]
    model_read_schema = openapi["components"]["schemas"]["ModelRead"]
    assert model_create_schema["properties"]["api_key"]["writeOnly"] is True
    assert "api_key" not in model_read_schema["properties"]
    _assert_secret_absent(CANARY, openapi)


@pytest.mark.parametrize(
    "invalid_key",
    [
        "",
        "201",
        "short",
        " leading-space",
        "trailing-space ",
        "line\r\nbreak",
        "unicode-密钥",
        "x" * 8193,
    ],
)
def test_invalid_web_api_keys_are_never_reflected(client, invalid_key: str) -> None:
    response = client.post("/api/v1/models", json=_stored_payload(api_key=invalid_key))

    assert response.status_code == 422
    if invalid_key:
        _assert_secret_absent(invalid_key, response.text, response.headers)
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Model)) == 0


def test_validation_rejects_secret_copies_and_hostile_field_names_without_reflection(
    client,
) -> None:
    duplicated = client.post(
        "/api/v1/models",
        json={**_stored_payload(), "remote_model_name": CANARY},
    )
    assert duplicated.status_code == 422
    _assert_secret_absent(CANARY, duplicated.text, duplicated.headers)

    extra_field = client.post(
        "/api/v1/models",
        json={"name": "Mock", "provider_type": "mock", CANARY: "value"},
    )
    assert extra_field.status_code == 422
    assert "<field>" in extra_field.text
    _assert_secret_absent(CANARY, extra_field.text, extra_field.headers)


@pytest.mark.parametrize(
    "fixed_public_value",
    ["adapter_type", "currency_assumption", "default_parameters"],
)
def test_api_key_cannot_match_fixed_run_snapshot_fields(
    client,
    fixed_public_value: str,
) -> None:
    response = client.post(
        "/api/v1/models",
        json=_stored_payload(api_key=fixed_public_value),
    )

    assert response.status_code == 422
    _assert_secret_absent(fixed_public_value, response.text, response.headers)
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Model)) == 0


@pytest.mark.parametrize(
    "public_copy",
    [
        {"default_parameters": {"seed": int(NUMERIC_CANARY)}},
        {"input_price_per_million": int(NUMERIC_CANARY)},
        {"output_price_per_million": int(NUMERIC_CANARY)},
    ],
)
def test_numeric_api_key_cannot_be_duplicated_into_public_numeric_fields(
    client,
    public_copy: dict[str, Any],
) -> None:
    response = client.post(
        "/api/v1/models",
        json={**_stored_payload(api_key=NUMERIC_CANARY), **public_copy},
    )

    assert response.status_code == 422
    _assert_secret_absent(NUMERIC_CANARY, response.text, response.headers)
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Model)) == 0


def test_missing_keyring_returns_stable_503_without_persisting_or_leaking(
    client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(get_settings(), "credential_keys_file", tmp_path / "missing.json")
    caplog.set_level("INFO")

    response = client.post("/api/v1/models", json=_stored_payload())

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "credential_store_unavailable",
            "message": "Encrypted credential storage is not available",
        }
    }
    _assert_secret_absent(CANARY, response.text, response.headers, caplog.text)
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Model)) == 0


def test_409_and_500_paths_do_not_reflect_api_keys(
    client,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _create_stored_model(client, name="Duplicate")
    conflict = client.post(
        "/api/v1/models",
        json=_stored_payload(name="Duplicate", api_key=ROTATED_CANARY),
    )
    assert conflict.status_code == 409
    _assert_secret_absent(ROTATED_CANARY, conflict.text, conflict.headers, caplog.text)

    def fail_after_validation(*_args: object, **_kwargs: object) -> EncryptedCredential:
        raise RuntimeError(ROTATED_CANARY)

    monkeypatch.setattr(models_api, "_encrypt_api_key", fail_after_validation)
    caplog.clear()
    caplog.set_level("ERROR")
    failed = client.post(
        "/api/v1/models",
        json=_stored_payload(name="Internal Failure", api_key=ROTATED_CANARY),
    )
    assert failed.status_code == 500
    assert failed.json()["detail"]["code"] == "internal_server_error"
    _assert_secret_absent(ROTATED_CANARY, failed.text, failed.headers, caplog.text)


def test_host_header_rebinding_is_rejected_before_credential_persistence(client) -> None:
    response = client.post(
        "/api/v1/models",
        json=_stored_payload(),
        headers={"Host": "attacker.example"},
    )

    assert response.status_code == 400
    _assert_secret_absent(CANARY, response.text, response.headers)
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Model)) == 0


def test_client_request_id_cannot_reflect_a_duplicated_api_key(
    client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")

    response = client.post(
        "/api/v1/models",
        json=_stored_payload(),
        headers={"X-Request-ID": CANARY},
    )

    assert response.status_code == 201
    assert response.headers["X-Request-ID"] != CANARY
    patched = client.patch(
        f"/api/v1/models/{response.json()['id']}",
        json={"api_key": ROTATED_CANARY},
        headers={"X-Request-ID": ROTATED_CANARY},
    )
    assert patched.status_code == 200
    assert patched.headers["X-Request-ID"] != ROTATED_CANARY
    rendered_logs = "\n".join(SanitizedJsonFormatter().format(record) for record in caplog.records)
    _assert_secret_absent(
        CANARY,
        response.text,
        response.headers,
        caplog.text,
        rendered_logs,
    )
    _assert_secret_absent(
        ROTATED_CANARY,
        patched.text,
        patched.headers,
        caplog.text,
        rendered_logs,
    )


def test_stored_credential_crud_origin_guard_active_run_lock_and_cleanup(client) -> None:
    created = _create_stored_model(client)
    model_id = created.json()["id"]
    original = _encrypted_row(model_id)

    preserved = client.patch(
        f"/api/v1/models/{model_id}",
        json={"enabled": False, "base_url": "https://provider.example/v2"},
    )
    assert preserved.status_code == 200, preserved.text
    preserved_row = _encrypted_row(model_id)
    assert preserved_row.nonce == original.nonce
    assert preserved_row.ciphertext == original.ciphertext
    assert _decrypt_row(preserved_row, base_url="https://provider.example/v2") == CANARY

    rejected_origin = client.patch(
        f"/api/v1/models/{model_id}",
        json={"base_url": "https://other-provider.example/v1"},
    )
    assert rejected_origin.status_code == 422
    assert rejected_origin.json()["detail"]["code"] == "api_key_required_for_origin_change"

    rotated = client.patch(
        f"/api/v1/models/{model_id}",
        json={
            "enabled": True,
            "base_url": "https://other-provider.example/v1",
            "api_key": ROTATED_CANARY,
        },
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["credential_source"] == "stored"
    rotated_row = _encrypted_row(model_id)
    assert (rotated_row.nonce, rotated_row.ciphertext) != (
        original.nonce,
        original.ciphertext,
    )
    assert _decrypt_row(rotated_row, base_url="https://other-provider.example/v1") == ROTATED_CANARY

    run = _create_pending_run(client, model_id)
    sensitive_updates = [
        {"remote_model_name": "changed-model"},
        {"base_url": "https://other-provider.example/v2"},
        {"base_url": "https://third-provider.example/v1"},
        {"api_key": CANARY},
        {"api_key_env": "LEGACY_PROVIDER_KEY"},
        {"provider_type": "mock"},
    ]
    for update in sensitive_updates:
        blocked = client.patch(f"/api/v1/models/{model_id}", json=update)
        assert blocked.status_code == 409, (update, blocked.text)
        assert blocked.json()["detail"]["code"] == "model_has_active_runs"
        _assert_secret_absent(CANARY, blocked.text, blocked.headers)

    cancelled = client.post(f"/api/v1/runs/{run['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    switched = client.patch(f"/api/v1/models/{model_id}", json={"provider_type": "mock"})
    assert switched.status_code == 200, switched.text
    assert switched.json()["credential_source"] == "none"
    assert switched.json()["has_api_key"] is False
    assert switched.json()["base_url"] is None
    with SessionLocal() as session:
        assert session.get(ModelCredential, model_id) is None


@pytest.mark.parametrize(
    "public_copy",
    [
        {"name": CANARY},
        {"base_url": f"https://provider.example/{CANARY}"},
        {"remote_model_name": CANARY},
    ],
)
def test_patch_without_reentering_key_rejects_existing_key_in_public_fields(
    client,
    public_copy: dict[str, Any],
) -> None:
    created = _create_stored_model(client)
    model_id = created.json()["id"]

    response = client.patch(f"/api/v1/models/{model_id}", json=public_copy)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "api_key_duplicated_in_public_fields"
    _assert_secret_absent(CANARY, response.text, response.headers)
    fetched = client.get(f"/api/v1/models/{model_id}")
    assert fetched.status_code == 200
    _assert_secret_absent(CANARY, fetched.text)


def test_patch_rejects_existing_key_as_legacy_environment_name(client) -> None:
    environment_shaped_key = "SECRET_PROVIDER_KEY"
    created = _create_stored_model(client, api_key=environment_shaped_key)
    model_id = created.json()["id"]

    response = client.patch(
        f"/api/v1/models/{model_id}",
        json={"api_key_env": environment_shaped_key},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "api_key_duplicated_in_public_fields"
    _assert_secret_absent(environment_shaped_key, response.text, response.headers)
    assert (
        _decrypt_row(
            _encrypted_row(model_id),
            base_url="https://provider.example/v1",
        )
        == environment_shaped_key
    )


def test_patch_rotation_cannot_copy_the_previous_key_into_public_fields(client) -> None:
    created = _create_stored_model(client)
    model_id = created.json()["id"]
    original = _encrypted_row(model_id)

    response = client.patch(
        f"/api/v1/models/{model_id}",
        json={"name": CANARY, "api_key": ROTATED_CANARY},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "api_key_duplicated_in_public_fields"
    _assert_secret_absent(CANARY, response.text, response.headers)
    _assert_secret_absent(ROTATED_CANARY, response.text, response.headers)
    persisted = _encrypted_row(model_id)
    assert persisted.nonce == original.nonce
    assert persisted.ciphertext == original.ciphertext
    assert _decrypt_row(persisted, base_url="https://provider.example/v1") == CANARY


@pytest.mark.parametrize("public_field", ["id", "created_at", "updated_at"])
def test_patch_key_cannot_match_generated_model_response_fields(
    client,
    public_field: str,
) -> None:
    created = _create_stored_model(client)
    model_id = created.json()["id"]
    generated_value = created.json()[public_field]

    response = client.patch(
        f"/api/v1/models/{model_id}",
        json={"api_key": generated_value},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "api_key_duplicated_in_public_fields"
    _assert_secret_absent(generated_value, response.text, response.headers)
    assert _decrypt_row(_encrypted_row(model_id), base_url="https://provider.example/v1") == CANARY


@pytest.mark.parametrize(
    ("damage", "update", "expected_source"),
    [
        ("missing", {"api_key": ROTATED_CANARY}, "stored"),
        ("unknown_key", {"api_key": ROTATED_CANARY}, "stored"),
        ("missing", {"provider_type": "mock"}, "none"),
        ("unknown_key", {"api_key_env": "RECOVERED_PROVIDER_KEY"}, "environment"),
    ],
)
def test_unreadable_stored_credential_can_be_replaced_or_removed(
    client,
    damage: str,
    update: dict[str, Any],
    expected_source: str,
) -> None:
    created = _create_stored_model(client)
    model_id = created.json()["id"]
    with SessionLocal() as session:
        credential = session.get(ModelCredential, model_id)
        assert credential is not None
        if damage == "missing":
            session.delete(credential)
        else:
            credential.key_id = "retired-key"
        session.commit()

    response = client.patch(f"/api/v1/models/{model_id}", json=update)

    assert response.status_code == 200, response.text
    assert response.json()["credential_source"] == expected_source
    _assert_secret_absent(ROTATED_CANARY, response.text, response.headers)
    if expected_source == "stored":
        assert (
            _decrypt_row(_encrypted_row(model_id), base_url="https://provider.example/v1")
            == ROTATED_CANARY
        )
    else:
        with SessionLocal() as session:
            assert session.get(ModelCredential, model_id) is None


def test_unreadable_stored_credential_still_fails_closed_when_preserved(client) -> None:
    created = _create_stored_model(client)
    model_id = created.json()["id"]
    with SessionLocal() as session:
        credential = session.get(ModelCredential, model_id)
        assert credential is not None
        credential.key_id = "retired-key"
        session.commit()

    response = client.patch(f"/api/v1/models/{model_id}", json={"enabled": False})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "credential_store_unavailable"
    _assert_secret_absent(CANARY, response.text, response.headers)


@pytest.mark.parametrize(
    "update",
    [
        {"api_key": ROTATED_CANARY, "name": CANARY},
        {"provider_type": "mock", "name": CANARY},
        {"api_key_env": "RECOVERED_PROVIDER_KEY", "name": CANARY},
    ],
)
def test_unreadable_credential_recovery_rejects_unrelated_public_changes(
    client,
    update: dict[str, Any],
) -> None:
    created = _create_stored_model(client)
    model_id = created.json()["id"]
    with SessionLocal() as session:
        credential = session.get(ModelCredential, model_id)
        assert credential is not None
        credential.key_id = "retired-key"
        session.commit()

    response = client.patch(f"/api/v1/models/{model_id}", json=update)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "credential_recovery_requires_isolated_update"
    _assert_secret_absent(CANARY, response.text, response.headers)
    fetched = client.get(f"/api/v1/models/{model_id}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Web Provider"
    _assert_secret_absent(CANARY, fetched.text)


@pytest.mark.parametrize(
    "invalid_base_url",
    [
        "https://provider.example:abc/v1",
        "https://provider.example:65536/v1",
    ],
)
def test_invalid_provider_ports_are_stable_validation_errors(
    client,
    invalid_base_url: str,
) -> None:
    created_with_invalid_url = client.post(
        "/api/v1/models",
        json={**_stored_payload(), "base_url": invalid_base_url},
    )
    assert created_with_invalid_url.status_code == 422
    _assert_secret_absent(CANARY, created_with_invalid_url.text, created_with_invalid_url.headers)

    created = _create_stored_model(client, name="Valid Provider")
    patched = client.patch(
        f"/api/v1/models/{created.json()['id']}",
        json={"base_url": invalid_base_url, "api_key": ROTATED_CANARY},
    )
    assert patched.status_code == 422
    _assert_secret_absent(ROTATED_CANARY, patched.text, patched.headers)


def test_environment_compatibility_can_transition_to_and_from_web_key(client) -> None:
    created = client.post(
        "/api/v1/models",
        json={
            "name": "Legacy Environment",
            "provider_type": "openai_compatible",
            "base_url": "https://provider.example/v1",
            "remote_model_name": "provider-model",
            "api_key_env": "LEGACY_PROVIDER_KEY",
        },
    )
    assert created.status_code == 201
    assert created.json()["credential_source"] == "environment"
    assert created.json()["has_api_key"] is False
    model_id = created.json()["id"]

    stored = client.patch(f"/api/v1/models/{model_id}", json={"api_key": CANARY})
    assert stored.status_code == 200, stored.text
    assert stored.json()["credential_source"] == "stored"
    assert stored.json()["api_key_env"] is None
    assert stored.json()["has_api_key"] is True

    explicit_null = client.patch(f"/api/v1/models/{model_id}", json={"api_key": None})
    assert explicit_null.status_code == 422
    assert _decrypt_row(_encrypted_row(model_id), base_url="https://provider.example/v1") == CANARY

    environment = client.patch(
        f"/api/v1/models/{model_id}", json={"api_key_env": "NEW_PROVIDER_KEY"}
    )
    assert environment.status_code == 200, environment.text
    assert environment.json()["credential_source"] == "environment"
    assert environment.json()["api_key_env"] == "NEW_PROVIDER_KEY"
    assert environment.json()["has_api_key"] is False
    with SessionLocal() as session:
        assert session.get(ModelCredential, model_id) is None


def test_failed_name_update_rolls_back_new_ciphertext_and_preserves_old_key(client) -> None:
    first = _create_stored_model(client, name="First")
    _create_stored_model(client, name="Taken", api_key=ROTATED_CANARY)
    model_id = first.json()["id"]
    original = _encrypted_row(model_id)

    response = client.patch(
        f"/api/v1/models/{model_id}",
        json={"name": "Taken", "api_key": ROTATED_CANARY},
    )

    assert response.status_code == 409
    _assert_secret_absent(ROTATED_CANARY, response.text, response.headers)
    persisted = _encrypted_row(model_id)
    assert persisted.nonce == original.nonce
    assert persisted.ciphertext == original.ciphertext
    assert _decrypt_row(persisted, base_url="https://provider.example/v1") == CANARY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    [
        "unknown_source",
        "none_with_environment",
        "environment_without_name",
        "stored_with_environment",
        "cross_model",
        "other_origin",
        "missing_credential",
    ],
)
async def test_worker_rejects_tampered_credential_snapshots_before_adapter_build(
    client,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    created = _create_stored_model(client)
    run_payload = _create_pending_run(client, created.json()["id"])
    run_id = run_payload["id"]

    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        assert run is not None
        snapshot = copy.deepcopy(run.model_parameters_snapshot)
        model = snapshot["model"]
        if tamper == "unknown_source":
            model["credential_source"] = "attacker-controlled"
            model["api_key_env"] = "REAL_PROVIDER_KEY"
        elif tamper == "none_with_environment":
            model["credential_source"] = "none"
            model["api_key_env"] = "REAL_PROVIDER_KEY"
        elif tamper == "environment_without_name":
            model["credential_source"] = "environment"
            model["api_key_env"] = None
        elif tamper == "stored_with_environment":
            model["credential_source"] = "stored"
            model["api_key_env"] = "REAL_PROVIDER_KEY"
        elif tamper == "cross_model":
            model["id"] = "different-model-id"
        elif tamper == "other_origin":
            model["base_url"] = "https://attacker.example/v1"
        elif tamper == "missing_credential":
            credential = session.get(ModelCredential, created.json()["id"])
            assert credential is not None
            session.delete(credential)
        run.model_parameters_snapshot = snapshot
        session.commit()

    monkeypatch.setenv("REAL_PROVIDER_KEY", ROTATED_CANARY)
    adapter_builds = 0

    def forbidden_adapter(*_args: object, **_kwargs: object) -> object:
        nonlocal adapter_builds
        adapter_builds += 1
        raise AssertionError("adapter must not be built for an invalid credential snapshot")

    monkeypatch.setattr(runner_module, "build_adapter", forbidden_adapter)
    caplog.set_level("ERROR", logger="app.runners.evaluation_runner")

    acknowledged = await EvaluationRunner(SessionLocal).execute(run_id)

    assert acknowledged is True
    assert adapter_builds == 0
    _assert_secret_absent(CANARY, caplog.text)
    _assert_secret_absent(ROTATED_CANARY, caplog.text)


def test_worker_decrypts_stored_key_and_wrong_keyring_fails_closed(
    client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created = _create_stored_model(client)
    run = _create_pending_run(client, created.json()["id"])
    runner = EvaluationRunner(SessionLocal)

    model, *_rest = runner._load_snapshots(run["id"])
    assert model.credential_source == "stored"
    assert model.api_key_env is None
    assert model.api_key is not None
    assert model.api_key.get_secret_value() == CANARY
    _assert_secret_absent(CANARY, model, repr(model))

    monkeypatch.setattr(runner._settings, "credential_keys_file", tmp_path / "missing.json")
    with pytest.raises(CredentialCryptoError) as caught:
        runner._load_snapshots(run["id"])
    assert str(caught.value) == "credential_keyring_unavailable"
    _assert_secret_absent(CANARY, caught.value, repr(caught.value))


@pytest.mark.asyncio
async def test_direct_stored_key_is_the_only_authorization_value_and_echoes_are_redacted() -> None:
    seen_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            json={
                "id": f"request-{CANARY}",
                "choices": [{"message": {"content": f"echo:{CANARY}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "provider-model",
            api_key=SecretStr(CANARY),
            client=http_client,
        )
        result = await adapter.generate([{"role": "user", "content": "hello"}], {})
        await adapter.aclose()

    assert seen_authorization == [f"Bearer {CANARY}"]
    assert result.text == "echo:[REDACTED]"
    _assert_secret_absent(CANARY, result, repr(result), adapter, repr(adapter))


@pytest.mark.asyncio
async def test_numeric_key_is_removed_from_usage_counts_and_raw_usage() -> None:
    seen_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            json={
                "id": "request-safe",
                "choices": [{"message": {"content": "safe"}}],
                "usage": {
                    "prompt_tokens": int(NUMERIC_CANARY),
                    "completion_tokens": 2,
                    "nested": [int(NUMERIC_CANARY)],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "provider-model",
            api_key=SecretStr(NUMERIC_CANARY),
            client=http_client,
        )
        result = await adapter.generate([{"role": "user", "content": "hello"}], {})
        await adapter.aclose()

    assert seen_authorization == [f"Bearer {NUMERIC_CANARY}"]
    assert result.input_tokens is None
    assert result.output_tokens == 2
    assert result.raw_usage == {
        "prompt_tokens": "[REDACTED]",
        "completion_tokens": 2,
        "nested": ["[REDACTED]"],
    }
    _assert_secret_absent(NUMERIC_CANARY, result, repr(result))


@pytest.mark.asyncio
async def test_numeric_key_matching_http_status_is_removed_from_error_evidence() -> None:
    numeric_status_canary = "401"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": numeric_status_canary}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        adapter = OpenAICompatibleAdapter(
            "https://provider.example/v1",
            "provider-model",
            api_key=SecretStr(numeric_status_canary),
            client=http_client,
            max_retries=0,
        )
        with pytest.raises(AdapterError) as caught:
            await adapter.generate([{"role": "user", "content": "hello"}], {})
        await adapter.aclose()

    assert caught.value.status_code is None
    _assert_secret_absent(
        numeric_status_canary,
        caught.value,
        repr(caught.value),
        caught.value.error_message,
        caught.value.status_code,
    )


@pytest.mark.asyncio
async def test_numeric_web_key_usage_never_reaches_persisted_run_evidence(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _create_stored_model(client, api_key=NUMERIC_CANARY)
    run_payload = _create_pending_run(client, created.json()["id"])
    authorizations: list[str | None] = []

    def provider(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            json={
                "id": NUMERIC_CANARY,
                "choices": [{"message": {"content": NUMERIC_CANARY}}],
                "usage": {
                    "prompt_tokens": int(NUMERIC_CANARY),
                    "completion_tokens": 2,
                },
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))

    def build_with_in_process_transport(provider_type: str, **kwargs: object):
        assert provider_type == "openai_compatible"
        return OpenAICompatibleAdapter(
            client=http_client,
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(runner_module, "build_adapter", build_with_in_process_transport)
    try:
        assert await EvaluationRunner(SessionLocal).execute(run_payload["id"]) is True
    finally:
        await http_client.aclose()

    assert len(authorizations) == run_payload["total_questions"]
    assert set(authorizations) == {f"Bearer {NUMERIC_CANARY}"}
    run_response = client.get(f"/api/v1/runs/{run_payload['id']}")
    responses = client.get(f"/api/v1/runs/{run_payload['id']}/responses?limit=100")
    leaderboard = client.get(f"/api/v1/leaderboard?model_id={created.json()['id']}&limit=100")
    assert run_response.status_code == responses.status_code == leaderboard.status_code == 200
    assert all(item["input_tokens"] is None for item in responses.json()["items"])
    assert all(item["raw_response"] == "[REDACTED]" for item in responses.json()["items"])
    _assert_secret_absent(
        NUMERIC_CANARY,
        run_response.text,
        responses.text,
        leaderboard.text,
    )

    database_path = Path(str(engine.url.database))
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{database_path}{suffix}")
        if candidate.exists():
            assert NUMERIC_CANARY.encode() not in candidate.read_bytes()


@pytest.mark.asyncio
async def test_run_snapshot_timeout_and_provider_default_reach_provider_request(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = client.post(
        "/api/v1/models",
        json={**_stored_payload(), "default_parameters": {"max_tokens": None}},
    )
    assert created.status_code == 201, created.text
    run_payload = _create_pending_run(
        client,
        created.json()["id"],
        read_timeout_seconds=321.5,
    )
    seen_payloads: list[dict[str, object]] = []
    seen_timeouts: list[dict[str, float]] = []
    adapter_kwargs: dict[str, object] = {}

    def provider(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        seen_timeouts.append(request.extensions["timeout"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "A"}, "finish_reason": "stop"}]},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))

    def build_with_in_process_transport(provider_type: str, **kwargs: object):
        assert provider_type == "openai_compatible"
        adapter_kwargs.update(kwargs)
        return OpenAICompatibleAdapter(
            client=http_client,
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(runner_module, "build_adapter", build_with_in_process_transport)
    try:
        assert await EvaluationRunner(SessionLocal).execute(run_payload["id"]) is True
    finally:
        await http_client.aclose()

    assert adapter_kwargs["read_timeout_seconds"] == 321.5
    assert len(seen_payloads) == run_payload["total_questions"]
    assert all("max_tokens" not in payload for payload in seen_payloads)
    assert all(payload["stream"] is True for payload in seen_payloads)
    assert all(payload["stream_options"] == {"include_usage": True} for payload in seen_payloads)
    assert all(timeout["read"] == 321.5 for timeout in seen_timeouts)


@pytest.mark.asyncio
async def test_stored_web_key_full_worker_and_report_path_never_persists_provider_echo(
    client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    created = _create_stored_model(client)
    run_payload = _create_pending_run(client, created.json()["id"])
    run_id = run_payload["id"]
    authorizations: list[str | None] = []

    def provider(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers.get("authorization"))
        secret_split = len(CANARY) // 2
        events = [
            {
                "id": f"provider-request-{CANARY}",
                "model": f"provider-model-{CANARY}",
                "system_fingerprint": f"fingerprint-{CANARY}",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": f"provider-echo:{CANARY[:secret_split]}"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": f"provider-request-{CANARY}",
                "model": f"provider-model-{CANARY}",
                "system_fingerprint": f"fingerprint-{CANARY}",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": CANARY[secret_split:]},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": f"provider-request-{CANARY}",
                "model": f"provider-model-{CANARY}",
                "system_fingerprint": f"fingerprint-{CANARY}",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": f"stop-{CANARY}",
                    }
                ],
            },
            {
                "id": f"provider-request-{CANARY}",
                "model": f"provider-model-{CANARY}",
                "system_fingerprint": f"fingerprint-{CANARY}",
                "choices": [],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    f"usage-{CANARY}": CANARY,
                },
            },
        ]
        response_body = (
            b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)
            + b"data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=response_body,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))

    def build_with_in_process_transport(provider_type: str, **kwargs: object):
        assert provider_type == "openai_compatible"
        api_key = kwargs.get("api_key")
        assert isinstance(api_key, SecretStr)
        assert api_key.get_secret_value() == CANARY
        assert kwargs.get("api_key_env") is None
        return OpenAICompatibleAdapter(
            client=http_client,
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(runner_module, "build_adapter", build_with_in_process_transport)
    caplog.set_level("INFO")
    try:
        assert await EvaluationRunner(SessionLocal).execute(run_id) is True
    finally:
        await http_client.aclose()

    assert len(authorizations) == run_payload["total_questions"]
    assert set(authorizations) == {f"Bearer {CANARY}"}
    run_response = client.get(f"/api/v1/runs/{run_id}")
    responses = client.get(f"/api/v1/runs/{run_id}/responses?limit=100")
    leaderboard = client.get(f"/api/v1/leaderboard?model_id={created.json()['id']}&limit=100")
    assert run_response.status_code == responses.status_code == leaderboard.status_code == 200
    assert run_response.json()["status"] == "completed"
    assert run_response.json()["input_tokens"] == run_payload["total_questions"]
    assert run_response.json()["output_tokens"] == run_payload["total_questions"]
    assert responses.json()["total"] == run_payload["total_questions"]
    assert all(item["input_tokens"] == 1 for item in responses.json()["items"])
    assert all(item["output_tokens"] == 1 for item in responses.json()["items"])
    assert all(
        item["raw_response"] == "provider-echo:[REDACTED]" for item in responses.json()["items"]
    )

    report_directory = tmp_path / "stored-key-report"
    with SessionLocal() as session:
        report = export_run_report(session, run_id, report_directory)
    report_bytes = b"".join(path.read_bytes() for path in report_directory.iterdir())
    assert report.response_count == run_payload["total_questions"]
    assert CANARY.encode() not in report_bytes

    rendered_logs = "\n".join(SanitizedJsonFormatter().format(record) for record in caplog.records)
    _assert_secret_absent(
        CANARY,
        run_response.text,
        responses.text,
        leaderboard.text,
        caplog.text,
        rendered_logs,
    )
    forbidden_public_fields = ('"ciphertext"', '"nonce"', '"key_id"', '"algorithm"')
    public_evidence = run_response.text + responses.text + leaderboard.text + report_bytes.decode()
    assert all(field not in public_evidence for field in forbidden_public_fields)

    database_path = Path(str(engine.url.database))
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{database_path}{suffix}")
        if candidate.exists():
            assert CANARY.encode() not in candidate.read_bytes()
