"""Read-only API coverage for retained, typed Run audit events."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.governance.audit import append_audit_event
from app.models import AuditEvent


def test_run_audit_is_filtered_paginated_and_stably_ordered(client, db_session) -> None:
    model = client.post(
        "/api/v1/models",
        json={"name": "Audit API Mock", "provider_type": "mock", "enabled": True},
    ).json()
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()
    run = client.post(
        "/api/v1/runs",
        json={"model_id": model["id"], "benchmark_id": benchmark["id"]},
    ).json()

    occurred_at = datetime(2026, 8, 27, 16, 0, tzinfo=UTC)
    append_audit_event(
        db_session,
        event_key=f"audit-api:{run['id']}:claimed",
        event_type="run_claimed",
        occurred_at=occurred_at,
        payload={"dispatch_count": 1},
        correlation_id=run["id"],
        run_id=run["id"],
        model_id=model["id"],
        worker_id="worker-audit-api",
        attempt=1,
        lease_token=1,
    )
    append_audit_event(
        db_session,
        event_key=f"audit-api:{run['id']}:yielded",
        event_type="run_yielded",
        occurred_at=occurred_at,
        payload={"responses_added": 2},
        correlation_id=run["id"],
        run_id=run["id"],
        model_id=model["id"],
        worker_id="worker-audit-api",
        attempt=1,
        lease_token=1,
    )
    append_audit_event(
        db_session,
        event_key=f"audit-api:{run['id']}:terminal",
        event_type="run_terminal",
        occurred_at=occurred_at + timedelta(seconds=1),
        payload={"status": "completed", "reason": "none"},
        correlation_id=run["id"],
        run_id=run["id"],
        model_id=model["id"],
    )
    append_audit_event(
        db_session,
        event_key="audit-api:unrelated:claimed",
        event_type="run_claimed",
        occurred_at=occurred_at,
        payload={"dispatch_count": 1},
        correlation_id="unrelated-run",
        run_id="unrelated-run",
        worker_id="worker-audit-api",
        attempt=1,
        lease_token=1,
    )
    db_session.commit()

    expected = list(
        db_session.scalars(
            select(AuditEvent)
            .where(AuditEvent.run_id == run["id"])
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )
    first = client.get(f"/api/v1/runs/{run['id']}/audit?offset=0&limit=2")
    second = client.get(f"/api/v1/runs/{run['id']}/audit?offset=2&limit=100")

    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == second.json()["total"] == len(expected) == 4
    assert first.json()["offset"] == 0
    assert first.json()["limit"] == 2
    assert second.json()["offset"] == 2
    assert [item["id"] for item in first.json()["items"] + second.json()["items"]] == [
        event.id for event in expected
    ]
    for item in first.json()["items"] + second.json()["items"]:
        assert item["run_id"] == run["id"]
        assert item["retention_class"] == "operational"
        assert "event_key" not in item
        assert "payload_hash" not in item


def test_run_audit_returns_the_standard_missing_run_error(client) -> None:
    response = client.get("/api/v1/runs/missing-run/audit")

    assert response.status_code == 404
    assert response.json() == {
        "detail": {"code": "run_not_found", "message": "Evaluation run was not found"}
    }


def test_run_audit_fails_closed_without_reflecting_a_corrupt_payload(
    client,
    db_session,
    caplog,
) -> None:
    secret = "sk-corrupt-audit-secret-value"
    model = client.post(
        "/api/v1/models",
        json={"name": "Corrupt Audit Mock", "provider_type": "mock", "enabled": True},
    ).json()
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()
    run = client.post(
        "/api/v1/runs",
        json={"model_id": model["id"], "benchmark_id": benchmark["id"]},
    ).json()
    event = append_audit_event(
        db_session,
        event_key=f"audit-api:{run['id']}:corrupt",
        event_type="run_claimed",
        occurred_at=datetime(2026, 8, 27, 17, 0, tzinfo=UTC),
        payload={"dispatch_count": 1},
        correlation_id=run["id"],
        run_id=run["id"],
        model_id=model["id"],
        worker_id="worker-audit-api",
        attempt=1,
        lease_token=1,
    )
    event.payload["dispatch_count"] = secret
    db_session.commit()
    caplog.set_level("ERROR", logger="app.api.v1.runs")

    response = client.get(f"/api/v1/runs/{run['id']}/audit")

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "audit_event_integrity_error",
            "message": "A retained audit event failed integrity validation",
        }
    }
    assert secret not in response.text
    assert secret not in caplog.text


@pytest.mark.parametrize("damage", ["payload_json", "occurred_at"])
def test_run_audit_classifies_invalid_stored_value_as_integrity_error(
    client,
    db_session,
    caplog,
    damage,
) -> None:
    marker = "sk-invalid-run-audit-json-never-reflect"
    model = client.post(
        "/api/v1/models",
        json={"name": "Invalid JSON Audit Mock", "provider_type": "mock", "enabled": True},
    ).json()
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()
    run = client.post(
        "/api/v1/runs",
        json={"model_id": model["id"], "benchmark_id": benchmark["id"]},
    ).json()
    occurred_at = datetime(2026, 8, 27, 17, 0, tzinfo=UTC)
    event = append_audit_event(
        db_session,
        event_key=f"audit-api:{run['id']}:invalid-json",
        event_type="run_claimed",
        occurred_at=occurred_at,
        payload={"dispatch_count": 1},
        run_id=run["id"],
        worker_id="worker-audit-api",
        attempt=1,
        lease_token=1,
    )
    event_id = event.id
    db_session.commit()
    if damage == "payload_json":
        db_session.connection().exec_driver_sql(
            "UPDATE audit_events SET payload = ? WHERE id = ?",
            ("{" + marker, event_id),
        )
    else:
        malformed_timestamp = occurred_at.replace(tzinfo=None).isoformat(sep=" ") + marker
        db_session.connection().exec_driver_sql(
            "UPDATE audit_events SET occurred_at = ? WHERE id = ?",
            (malformed_timestamp, event_id),
        )
    db_session.commit()
    caplog.set_level("ERROR", logger="app.api.v1.runs")

    response = client.get(f"/api/v1/runs/{run['id']}/audit")

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "audit_event_integrity_error",
            "message": "A retained audit event failed integrity validation",
        }
    }
    assert marker not in response.text
    assert marker not in caplog.text


def test_run_audit_rejects_noncanonical_stored_payload_representation(
    client,
    db_session,
) -> None:
    model = client.post(
        "/api/v1/models",
        json={"name": "Canonical Audit Mock", "provider_type": "mock", "enabled": True},
    ).json()
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()
    run = client.post(
        "/api/v1/runs",
        json={"model_id": model["id"], "benchmark_id": benchmark["id"]},
    ).json()
    event = append_audit_event(
        db_session,
        event_key=f"audit-api:{run['id']}:payload-canonical",
        event_type="provider_attempt_settled",
        occurred_at=datetime(2026, 8, 27, 17, 0, tzinfo=UTC),
        payload={
            "disposition": "settled_actual",
            "outcome": "succeeded",
            "input_tokens": 1,
            "output_tokens": 1,
            "cost_usd": "1",
            "reconciled": False,
        },
        correlation_id=run["id"],
        run_id=run["id"],
        model_id=model["id"],
        provider_attempt=1,
    )
    event.payload["cost_usd"] = 1
    db_session.commit()

    response = client.get(f"/api/v1/runs/{run['id']}/audit")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "audit_event_integrity_error"


def test_run_audit_rejects_nonfinite_retained_duration(client, db_session) -> None:
    model = client.post(
        "/api/v1/models",
        json={"name": "Nonfinite Audit Mock", "provider_type": "mock", "enabled": True},
    ).json()
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()
    run = client.post(
        "/api/v1/runs",
        json={"model_id": model["id"], "benchmark_id": benchmark["id"]},
    ).json()
    event = append_audit_event(
        db_session,
        event_key=f"audit-api:{run['id']}:nonfinite",
        event_type="run_claimed",
        occurred_at=datetime(2026, 8, 27, 17, 0, tzinfo=UTC),
        payload={"dispatch_count": 1},
        correlation_id=run["id"],
        run_id=run["id"],
        model_id=model["id"],
        duration_ms=1,
    )
    event.duration_ms = float("inf")
    db_session.commit()

    response = client.get(f"/api/v1/runs/{run['id']}/audit")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "audit_event_integrity_error"


def test_run_audit_validates_non_response_storage_identities(client, db_session, caplog) -> None:
    marker = "https://secret.example.invalid/audit-event-key"
    model = client.post(
        "/api/v1/models",
        json={"name": "Identity Audit Mock", "provider_type": "mock", "enabled": True},
    ).json()
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()
    run = client.post(
        "/api/v1/runs",
        json={"model_id": model["id"], "benchmark_id": benchmark["id"]},
    ).json()
    event = append_audit_event(
        db_session,
        event_key=f"audit-api:{run['id']}:identity",
        event_type="run_claimed",
        occurred_at=datetime(2026, 8, 27, 17, 0, tzinfo=UTC),
        payload={"dispatch_count": 1},
        run_id=run["id"],
    )
    event.event_key = marker
    db_session.commit()
    caplog.set_level("ERROR", logger="app.api.v1.runs")

    response = client.get(f"/api/v1/runs/{run['id']}/audit")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "audit_event_integrity_error"
    assert marker not in response.text
    assert marker not in caplog.text
