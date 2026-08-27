"""Governance corruption is fail-closed and produces secret-free evidence."""

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.governance import GovernanceIntegrityError, GovernanceRepository
from app.models import AuditEvent
from app.runners.run_leases import RunLeaseRepository
from app.workers.service import WorkerService


def _integrity_events() -> list[AuditEvent]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == "governance_integrity_error")
            )
        )


def test_policy_read_integrity_failure_is_stable_and_audited(client, monkeypatch) -> None:
    def fail_policy(*_args, **_kwargs):
        raise GovernanceIntegrityError("sensitive-database-value")

    monkeypatch.setattr(GovernanceRepository, "active_policy", fail_policy)

    response = client.get("/api/v1/governance/policy")

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "governance_integrity_error",
        "message": "Governance state failed integrity validation.",
    }
    assert "sensitive-database-value" not in response.text
    events = _integrity_events()
    assert len(events) == 1
    assert events[0].payload == {"reason": "governance_integrity_error"}


def test_run_admission_integrity_failure_is_stable_and_audited(client, monkeypatch) -> None:
    model = client.post(
        "/api/v1/models",
        json={"name": "Governed Mock", "provider_type": "mock"},
    ).json()
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()

    def fail_admission(*_args, **_kwargs):
        raise GovernanceIntegrityError("sensitive-ledger-value")

    monkeypatch.setattr(GovernanceRepository, "admit_run", fail_admission)

    response = client.post(
        "/api/v1/runs",
        json={"model_id": model["id"], "benchmark_id": benchmark["id"]},
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "governance_integrity_error"
    assert "sensitive-ledger-value" not in response.text
    events = _integrity_events()
    assert len(events) == 1
    # The rejected Run has never been flushed, so no durable Run identity exists.
    assert events[0].run_id is None
    assert events[0].model_id == model["id"]


def test_worker_reaper_integrity_failure_pauses_and_is_audited(
    client,
    monkeypatch,
) -> None:
    del client  # Initialize the isolated schema.
    repository = RunLeaseRepository(SessionLocal)

    def fail_reap(*_args, **_kwargs):
        raise GovernanceIntegrityError("sensitive-counter-value")

    monkeypatch.setattr(repository, "reap_expired", fail_reap)
    service = WorkerService(
        SessionLocal,
        get_settings(),
        run_queue=None,
        worker_id="worker:integrity-test",
        lease_repository=repository,
    )

    assert service._reap_expired() is None
    events = _integrity_events()
    assert len(events) == 1
    assert events[0].worker_id == "worker:integrity-test"
    assert events[0].payload == {"reason": "governance_integrity_error"}
