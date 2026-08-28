"""Typed, secret-free audit coverage for Provider credential lifecycle events."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

import app.services.credential_audit as credential_audit_module
from app.db.session import SessionLocal
from app.models import AuditEvent, Model, ModelCredential

CANARY = "sk-audit-canary-A7vN4xQ2pL9mT6rK3dW8"
ROTATED_CANARY = "sk-audit-rotated-H8qS5nC1yM4kR7vP2tX9"


def _stored_payload(*, name: str, api_key: str = CANARY) -> dict[str, Any]:
    return {
        "name": name,
        "provider_type": "openai_compatible",
        "base_url": "https://provider.example/v1",
        "remote_model_name": "provider-model",
        "api_key": api_key,
        "enabled": True,
    }


def _create_stored_model(client, *, name: str) -> dict[str, Any]:
    response = client.post("/api/v1/models", json=_stored_payload(name=name))
    assert response.status_code == 201, response.text
    return response.json()


def _create_pending_run(client, model_id: str) -> dict[str, Any]:
    benchmark = client.post("/api/v1/benchmarks/reload-demo")
    assert benchmark.status_code in {200, 201}, benchmark.text
    response = client.post(
        "/api/v1/runs",
        json={"model_id": model_id, "benchmark_id": benchmark.json()["id"]},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _credential_event_facts(model_id: str) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        events = list(
            session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.model_id == model_id,
                    AuditEvent.event_type.in_(
                        (
                            "credential_changed",
                            "credential_rejected",
                            "credential_decrypt_failed",
                        )
                    ),
                )
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        )
        return [
            {
                "event_type": event.event_type,
                "payload": dict(event.payload),
                "retention_class": event.retention_class.value,
                "retention": event.expires_at - event.occurred_at,
                "model_id": event.model_id,
                "run_id": event.run_id,
                "question_id": event.question_id,
                "worker_id": event.worker_id,
                "reservation_id": event.reservation_id,
            }
            for event in events
        ]


def _assert_secret_free(events: list[dict[str, Any]]) -> None:
    rendered = json.dumps(events, sort_keys=True, default=str)
    assert CANARY not in rendered
    assert ROTATED_CANARY not in rendered
    assert "provider.example" not in rendered
    for event in events:
        payload = event["payload"]
        assert not ({"api_key", "ciphertext", "nonce", "base_url", "payload"} & payload.keys())
        assert event["retention_class"] == "security"
        assert event["retention"] == timedelta(days=365)
        assert event["run_id"] is None
        assert event["question_id"] is None
        assert event["worker_id"] is None
        assert event["reservation_id"] is None


def test_successful_credential_lifecycle_appends_typed_security_events(client) -> None:
    first = _create_stored_model(client, name="Credential Audit Lifecycle")
    model_id = first["id"]

    replaced = client.patch(
        f"/api/v1/models/{model_id}",
        json={"api_key": ROTATED_CANARY},
    )
    assert replaced.status_code == 200, replaced.text
    switched = client.patch(
        f"/api/v1/models/{model_id}",
        json={"api_key_env": "AUDIT_PROVIDER_KEY"},
    )
    assert switched.status_code == 200, switched.text

    second = _create_stored_model(client, name="Credential Audit Removal")
    removed = client.patch(
        f"/api/v1/models/{second['id']}",
        json={"provider_type": "mock"},
    )
    assert removed.status_code == 200, removed.text

    first_events = _credential_event_facts(model_id)
    first_changes = [
        event["payload"] for event in first_events if event["event_type"] == "credential_changed"
    ]
    assert Counter(
        (payload["action"], payload["credential_source"], payload["key_id"])
        for payload in first_changes
    ) == Counter(
        {
            ("created", "stored", "fixture-v1"): 1,
            ("replaced", "stored", "fixture-v1"): 1,
            ("source_switched", "environment", "fixture-v1"): 1,
        }
    )
    second_events = _credential_event_facts(second["id"])
    second_changes = [
        event["payload"] for event in second_events if event["event_type"] == "credential_changed"
    ]
    assert Counter(
        (payload["action"], payload["credential_source"], payload["key_id"])
        for payload in second_changes
    ) == Counter(
        {
            ("created", "stored", "fixture-v1"): 1,
            ("removed", "none", "fixture-v1"): 1,
        }
    )
    _assert_secret_free(first_events + second_events)


def test_credential_audit_timestamp_uses_database_clock(client, monkeypatch) -> None:
    monkeypatch.setattr(
        credential_audit_module,
        "utc_now",
        lambda: datetime(2099, 1, 1, tzinfo=UTC),
        raising=False,
    )
    created = _create_stored_model(client, name="Credential Audit Database Clock")

    with SessionLocal() as session:
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.model_id == created["id"],
                AuditEvent.event_type == "credential_changed",
            )
        )
        database_now = session.scalar(select(func.current_timestamp()))
        assert event is not None
        assert isinstance(database_now, datetime)
        if database_now.tzinfo is None:
            database_now = database_now.replace(tzinfo=UTC)
        assert abs((event.occurred_at - database_now).total_seconds()) < 5


def test_origin_and_active_run_rejections_commit_security_audit_after_rollback(client) -> None:
    created = _create_stored_model(client, name="Credential Audit Rejections")
    model_id = created["id"]

    origin_rejected = client.patch(
        f"/api/v1/models/{model_id}",
        json={"base_url": "https://other-provider.example/v1"},
    )
    assert origin_rejected.status_code == 422, origin_rejected.text
    assert origin_rejected.json()["detail"]["code"] == "api_key_required_for_origin_change"

    _create_pending_run(client, model_id)
    active_run_rejected = client.patch(
        f"/api/v1/models/{model_id}",
        json={"api_key": ROTATED_CANARY},
    )
    assert active_run_rejected.status_code == 409, active_run_rejected.text
    assert active_run_rejected.json()["detail"]["code"] == "model_has_active_runs"

    events = _credential_event_facts(model_id)
    rejected = [
        event["payload"] for event in events if event["event_type"] == "credential_rejected"
    ]
    assert Counter(
        (payload["reason"], payload["credential_source"], payload["key_id"]) for payload in rejected
    ) == Counter(
        {
            ("origin_rejected", "stored", "fixture-v1"): 1,
            ("active_run_conflict", "stored", "fixture-v1"): 1,
        }
    )
    with SessionLocal() as session:
        model = session.get(Model, model_id)
        assert model is not None
        assert model.base_url == "https://provider.example/v1"
        assert model.credential_source.value == "stored"
    _assert_secret_free(events)


def test_active_run_rejection_precedes_unreadable_credential_decryption(client) -> None:
    created = _create_stored_model(client, name="Credential Audit Guard Ordering")
    model_id = created["id"]
    with SessionLocal() as session, session.begin():
        credential = session.get(ModelCredential, model_id)
        assert credential is not None
        credential.key_id = "missing-credential-key"

    _create_pending_run(client, model_id)
    rejected = client.patch(
        f"/api/v1/models/{model_id}",
        json={"api_key": ROTATED_CANARY},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "model_has_active_runs"

    events = _credential_event_facts(model_id)
    assert [event for event in events if event["event_type"] == "credential_decrypt_failed"] == []
    assert any(
        event["event_type"] == "credential_rejected"
        and event["payload"]
        == {
            "reason": "active_run_conflict",
            "credential_source": "stored",
            "key_id": "missing-credential-key",
        }
        for event in events
    )
    _assert_secret_free(events)


def test_decrypt_failure_audit_survives_rejection_and_commits_with_recovery(client) -> None:
    created = _create_stored_model(client, name="Credential Audit Decrypt Failure")
    model_id = created["id"]
    with SessionLocal() as session, session.begin():
        credential = session.get(ModelCredential, model_id)
        assert credential is not None
        # A compromised row may place credential-shaped text in an identifier
        # column.  Audit normalization must still fail closed.
        credential.key_id = CANARY

    rejected = client.patch(f"/api/v1/models/{model_id}", json={"enabled": False})
    assert rejected.status_code == 503, rejected.text
    assert rejected.json()["detail"]["code"] == "credential_store_unavailable"
    after_rejection = _credential_event_facts(model_id)
    decrypt_failures = [
        event["payload"]
        for event in after_rejection
        if event["event_type"] == "credential_decrypt_failed"
    ]
    assert decrypt_failures == [{"reason": "decrypt_failed", "key_id": None}]
    with SessionLocal() as session:
        model = session.get(Model, model_id)
        assert model is not None and model.enabled is True

    recovered = client.patch(
        f"/api/v1/models/{model_id}",
        json={"api_key": ROTATED_CANARY},
    )
    assert recovered.status_code == 200, recovered.text
    events = _credential_event_facts(model_id)
    decrypt_failures = [
        event["payload"] for event in events if event["event_type"] == "credential_decrypt_failed"
    ]
    assert decrypt_failures == [
        {"reason": "decrypt_failed", "key_id": None},
        {"reason": "decrypt_failed", "key_id": None},
    ]
    changed = [event["payload"] for event in events if event["event_type"] == "credential_changed"]
    assert any(
        payload
        == {
            "action": "replaced",
            "credential_source": "stored",
            "key_id": "fixture-v1",
        }
        for payload in changed
    )
    _assert_secret_free(events)
