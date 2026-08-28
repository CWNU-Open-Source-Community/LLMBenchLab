"""Read-only API coverage for retained, typed Run audit events."""

from datetime import UTC, datetime, timedelta

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
