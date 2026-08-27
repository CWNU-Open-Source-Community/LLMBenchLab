"""Explicit governance policy activation and admission API evidence."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import AuditEvent, EvaluationRun, GovernancePolicy, GovernanceScope
from app.schemas.evaluation_run import EvaluationRunRead


def _full_policy(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "global_concurrency_limit": None,
        "provider_concurrency_limit": None,
        "model_concurrency_limit": None,
        "run_concurrency_limit": None,
        "global_requests_per_minute": None,
        "provider_requests_per_minute": None,
        "model_requests_per_minute": None,
        "run_requests_per_minute": None,
        "global_tokens_per_minute": None,
        "provider_tokens_per_minute": None,
        "model_tokens_per_minute": None,
        "run_tokens_per_minute": None,
        "global_lifetime_request_budget": None,
        "global_lifetime_token_budget": None,
        "global_lifetime_cost_budget_usd": None,
        "run_lifetime_request_budget": None,
        "run_lifetime_token_budget": None,
        "run_lifetime_cost_budget_usd": None,
        "backlog_limit": 1000,
        "question_quantum": 25,
    }
    values.update(overrides)
    return values


def test_policy_apply_is_idempotent_and_frozen_into_run_admission(client, monkeypatch) -> None:
    missing = client.get("/api/v1/governance/policy")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"
    assert missing.json()["detail"]["code"] == "governance_policy_not_initialized"
    with SessionLocal() as session:
        assert session.scalar(select(func.count(GovernancePolicy.id))) == 0
        assert session.scalar(select(func.count(GovernanceScope.id))) == 0
        assert session.scalar(select(func.count(AuditEvent.id))) == 0

    payload = _full_policy(
        global_concurrency_limit=2,
        global_requests_per_minute=10,
        backlog_limit=1,
        question_quantum=1,
    )
    applied = client.put("/api/v1/governance/policy", json=payload)
    replay = client.put("/api/v1/governance/policy", json=payload)
    assert applied.status_code == replay.status_code == 200
    assert applied.json() == replay.json()
    assert applied.json()["version"] == 2
    assert applied.json()["global_concurrency_limit"] == 2
    assert len(applied.json()["policy_hash"]) == 64
    read_back = client.get("/api/v1/governance/policy")
    assert read_back.status_code == 200
    assert read_back.json() == applied.json()

    model = client.post(
        "/api/v1/models",
        json={"name": "Governed Mock", "provider_type": "mock"},
    ).json()
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()
    first = client.post(
        "/api/v1/runs",
        json={"model_id": model["id"], "benchmark_id": benchmark["id"]},
    )
    assert first.status_code == 202

    rollback_calls = 0
    original_rollback = Session.rollback

    def tracked_rollback(session: Session) -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        original_rollback(session)

    monkeypatch.setattr(Session, "rollback", tracked_rollback)
    blocked = client.post(
        "/api/v1/runs",
        json={"model_id": model["id"], "benchmark_id": benchmark["id"]},
    )
    assert blocked.status_code == 429
    assert rollback_calls >= 1
    assert blocked.json()["detail"] == {
        "code": "run_backlog_full",
        "message": "The managed Run backlog is at its configured limit.",
        "limit": 1,
    }

    with SessionLocal() as session:
        run = session.get(EvaluationRun, first.json()["id"])
        policy_count = session.scalar(select(func.count(GovernancePolicy.id)))
        applied_events = session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.event_type == "governance_policy_applied"
            )
        )
        assert run is not None
        snapshot = run.model_parameters_snapshot["governance"]
        assert snapshot["policy_id"] == applied.json()["id"]
        assert snapshot["policy_hash"] == applied.json()["policy_hash"]
        assert snapshot["question_quantum"] == 1
        assert policy_count == 2
        assert applied_events == 1


def test_run_cost_budget_response_schema_round_trip_preserves_eight_decimal_places(client) -> None:
    budget = "9999999.12345678"
    model = client.post(
        "/api/v1/models",
        json={"name": "Precise Budget Mock", "provider_type": "mock"},
    ).json()
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()

    created = client.post(
        "/api/v1/runs",
        json={
            "model_id": model["id"],
            "benchmark_id": benchmark["id"],
            "lifetime_cost_budget_usd": budget,
        },
    )

    assert created.status_code == 202, created.text
    assert created.json()["lifetime_cost_budget_usd"] == budget
    assert (
        created.json()["model_parameters_snapshot"]["governance"]["run_overrides"][
            "lifetime_cost_budget_usd"
        ]
        == budget
    )

    response_schema = EvaluationRunRead.model_validate_json(created.text)
    assert response_schema.lifetime_cost_budget_usd == Decimal(budget)
    assert f'"lifetime_cost_budget_usd":"{budget}"' in response_schema.model_dump_json()

    reloaded = client.get(f"/api/v1/runs/{created.json()['id']}")
    assert reloaded.status_code == 200
    assert reloaded.json()["lifetime_cost_budget_usd"] == budget

    rejected = client.post(
        "/api/v1/runs",
        json={
            "model_id": model["id"],
            "benchmark_id": benchmark["id"],
            "lifetime_cost_budget_usd": "10000000.00000001",
        },
    )
    assert rejected.status_code == 422


def test_policy_apply_reactivates_content_addressed_revision(client) -> None:
    policy_a = _full_policy(backlog_limit=111, question_quantum=7)
    policy_b = _full_policy(backlog_limit=222, question_quantum=9)

    applied_a = client.put("/api/v1/governance/policy", json=policy_a)
    applied_b = client.put("/api/v1/governance/policy", json=policy_b)
    reactivated_a = client.put("/api/v1/governance/policy", json=policy_a)

    assert applied_a.status_code == applied_b.status_code == reactivated_a.status_code == 200
    a_document = applied_a.json()
    b_document = applied_b.json()
    reactivated_document = reactivated_a.json()
    assert (a_document["version"], b_document["version"]) == (2, 3)
    assert reactivated_document["id"] == a_document["id"]
    assert reactivated_document["version"] == a_document["version"]
    assert reactivated_document["policy_hash"] == a_document["policy_hash"]
    assert reactivated_document["backlog_limit"] == 111
    assert reactivated_document["question_quantum"] == 7
    assert reactivated_document["is_active"] is True
    assert client.get("/api/v1/governance/policy").json() == reactivated_document

    with SessionLocal() as session:
        policies = list(
            session.scalars(select(GovernancePolicy).order_by(GovernancePolicy.version))
        )
        applied_events = list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == "governance_policy_applied")
            )
        )
        assert len(policies) == 3
        assert [policy.id for policy in policies if policy.is_active] == [a_document["id"]]
        assert policies[1].backlog_limit == 111
        assert policies[1].question_quantum == 7
        assert policies[2].id == b_document["id"]
        assert policies[2].is_active is False
        assert sorted(event.payload["policy_version"] for event in applied_events) == [2, 2, 3]


def test_concurrent_policy_put_responses_preserve_each_linearization_snapshot(
    client,
    monkeypatch,
) -> None:
    original_commit = Session.commit
    committed = Barrier(2)

    def commit_then_wait_for_competing_put(session: Session) -> None:
        original_commit(session)
        committed.wait(timeout=5)

    monkeypatch.setattr(Session, "commit", commit_then_wait_for_competing_put)
    policies = (
        _full_policy(backlog_limit=111, question_quantum=7),
        _full_policy(backlog_limit=222, question_quantum=9),
    )

    def apply(policy: dict[str, object]):
        return client.put("/api/v1/governance/policy", json=policy)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply, policy) for policy in policies]
        responses = [future.result(timeout=10) for future in futures]

    assert [response.status_code for response in responses] == [200, 200]
    documents = [response.json() for response in responses]
    assert [document["backlog_limit"] for document in documents] == [111, 222]
    assert [document["is_active"] for document in documents] == [True, True]
    assert {document["version"] for document in documents} == {2, 3}


def test_policy_bounds_and_zero_deny_semantics_are_stable(client) -> None:
    invalid = client.put(
        "/api/v1/governance/policy",
        json=_full_policy(global_concurrency_limit=-1),
    )
    assert invalid.status_code == 422

    assert client.put("/api/v1/governance/policy", json={"backlog_limit": 5}).status_code == 422
    for invalid_value in (True, 1.0):
        response = client.put(
            "/api/v1/governance/policy",
            json=_full_policy(global_concurrency_limit=invalid_value),
        )
        assert response.status_code == 422
    for field, invalid_value in (
        ("global_concurrency_limit", 2**31),
        ("global_requests_per_minute", 2**63),
        ("global_lifetime_cost_budget_usd", "10000000.00000001"),
    ):
        response = client.put(
            "/api/v1/governance/policy",
            json=_full_policy(**{field: invalid_value}),
        )
        assert response.status_code == 422

    denied = client.put(
        "/api/v1/governance/policy",
        json=_full_policy(backlog_limit=0, question_quantum=1),
    )
    assert denied.status_code == 200
    assert denied.json()["backlog_limit"] == 0

    model = client.post(
        "/api/v1/models",
        json={"name": "Denied Mock", "provider_type": "mock"},
    ).json()
    benchmark = client.post("/api/v1/benchmarks/reload-demo").json()
    blocked = client.post(
        "/api/v1/runs",
        json={"model_id": model["id"], "benchmark_id": benchmark["id"]},
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "run_backlog_full"
    assert blocked.json()["detail"]["limit"] == 0


def test_governance_policy_put_is_cors_preflight_allowed(client) -> None:
    response = client.options(
        "/api/v1/governance/policy",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert "PUT" in response.headers["access-control-allow-methods"]
