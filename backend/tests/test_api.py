"""Versioned API and secret-safety integration tests."""

from __future__ import annotations

import os

import pytest

from app.models import Model


def test_health_and_info_do_not_require_provider(client) -> None:
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    info = client.get("/api/v1/info")
    assert info.status_code == 200
    assert info.json()["protocol_version"] == "llmbenchlab-protocol-v1"


def test_model_crud_pagination_and_secret_value_not_returned(client) -> None:
    os.environ["TEST_PROVIDER_KEY"] = "secret-value-must-never-appear"
    created = client.post(
        "/api/v1/models",
        json={
            "name": "Compatible Test",
            "provider_type": "openai_compatible",
            "base_url": "https://models.invalid/v1",
            "remote_model_name": "offline-test-model",
            "api_key_env": "TEST_PROVIDER_KEY",
            "enabled": True,
        },
    )
    assert created.status_code == 201
    assert created.json()["api_key_env"] == "TEST_PROVIDER_KEY"
    assert created.json()["input_price_per_million"] is None
    assert created.json()["output_price_per_million"] is None
    assert "secret-value-must-never-appear" not in created.text

    model_id = created.json()["id"]
    updated = client.patch(f"/api/v1/models/{model_id}", json={"enabled": False})
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    listed = client.get("/api/v1/models?offset=0&limit=1")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == model_id
    deleted = client.delete(f"/api/v1/models/{model_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/models/{model_id}").status_code == 404


def test_openai_model_provider_fields_are_validated(client) -> None:
    response = client.post(
        "/api/v1/models",
        json={"name": "Incomplete", "provider_type": "openai_compatible"},
    )
    assert response.status_code == 422
    assert "base_url" in response.text


def test_mock_model_rejects_remote_connection_fields(client) -> None:
    response = client.post(
        "/api/v1/models",
        json={
            "name": "Unsafe Mock",
            "provider_type": "mock",
            "base_url": "https://models.invalid/v1",
            "remote_model_name": "not-used",
            "api_key_env": "TEST_PROVIDER_KEY",
        },
    )
    assert response.status_code == 422
    assert "mock requires empty" in response.text


def test_model_rejects_secret_bypass_fields_without_reflecting_values(client) -> None:
    fake_secret = "credential-value-that-must-not-be-reflected"
    sensitive_keys = (
        "token",
        "api_token",
        "apiToken",
        "clientSecret",
        "private_key",
        "credential",
        "x_token",
        "nested",
    )
    for index, sensitive_key in enumerate(sensitive_keys):
        default_parameter_response = client.post(
            "/api/v1/models",
            json={
                "name": f"Unsafe Parameters {index}",
                "provider_type": "mock",
                "default_parameters": {sensitive_key: fake_secret},
            },
        )
        assert default_parameter_response.status_code == 422
        assert "only supports" in default_parameter_response.text
        assert fake_secret not in default_parameter_response.text

    query_response = client.post(
        "/api/v1/models",
        json={
            "name": "Unsafe URL",
            "provider_type": "openai_compatible",
            "base_url": f"https://models.invalid/v1?token={fake_secret}",
            "remote_model_name": "test-model",
            "api_key_env": "TEST_PROVIDER_KEY",
        },
    )
    assert query_response.status_code == 422
    assert "query parameters" in query_response.text
    assert fake_secret not in query_response.text


@pytest.mark.parametrize("price_literal", ["Infinity", "-Infinity", "NaN"])
def test_model_rejects_non_finite_prices(client, price_literal: str) -> None:
    response = client.post(
        "/api/v1/models",
        content=(
            '{"name":"Invalid Price","provider_type":"mock",'
            f'"input_price_per_million":{price_literal}}}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "finite number" in response.text


@pytest.mark.parametrize("number_literal", ["Infinity", "-Infinity", "NaN"])
def test_model_rejects_non_finite_default_parameters(client, number_literal: str) -> None:
    response = client.post(
        "/api/v1/models",
        content=(
            '{"name":"Invalid Defaults","provider_type":"mock",'
            f'"default_parameters":{{"temperature":{number_literal}}}}}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "finite number" in response.text


def test_model_rejects_null_for_nonnullable_default_parameters(client) -> None:
    for index, field in enumerate(("temperature", "top_p", "max_tokens")):
        response = client.post(
            "/api/v1/models",
            json={
                "name": f"Null Default {index}",
                "provider_type": "mock",
                "default_parameters": {field: None},
            },
        )
        assert response.status_code == 422
        assert f"default_parameters.{field} must not be null" in response.text


def test_run_concurrency_cannot_exceed_runner_limit(client) -> None:
    response = client.post(
        "/api/v1/runs",
        json={
            "model_id": "model-id",
            "benchmark_id": "benchmark-id",
            "concurrency": 5,
        },
    )
    assert response.status_code == 422
    assert "concurrency" in response.text


def test_database_crud_round_trip(db_session) -> None:
    row = Model(name="Database Mock", provider_type="mock", default_parameters={})
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    assert db_session.get(Model, row.id).name == "Database Mock"
