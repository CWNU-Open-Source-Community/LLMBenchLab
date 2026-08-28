"""Static contract checks for the repository-owned Phase 2 alert rules."""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_RULES = _ROOT / "deploy" / "observability" / "prometheus-alerts.json"
_SCRAPE = _ROOT / "deploy" / "observability" / "prometheus-scrape.example.yml"
_OPERATIONS = _ROOT / "docs" / "OPERATIONS.md"

_EXPECTED_RULES = {
    "LLMBenchLabMetricsUnavailable": (
        'up{job="llmbenchlab"} == 0',
        "2m",
        "critical",
        "alert-exporter-unavailable",
    ),
    "LLMBenchLabBacklogPersistent": (
        "llmbenchlab_runs_due_pending > 0",
        "15m",
        "warning",
        "alert-backlog-persistent",
    ),
    "LLMBenchLabDeadLettered": (
        'llmbenchlab_audit_events_window{event_type="run_dead_lettered"} > 0',
        "0s",
        "critical",
        "alert-dead-letter",
    ),
    "LLMBenchLabGovernanceIntegrityError": (
        'llmbenchlab_audit_events_window{event_type="governance_integrity_error"} > 0',
        "0s",
        "critical",
        "alert-governance-integrity",
    ),
    "LLMBenchLabGovernanceOverdrawn": (
        "llmbenchlab_governance_scopes_overdrawn > 0",
        "0s",
        "critical",
        "alert-governance-overdraw",
    ),
    "LLMBenchLabQueueDegraded": (
        "llmbenchlab_queue_configured == 1 and llmbenchlab_queue_available == 0",
        "2m",
        "warning",
        "alert-queue-degraded",
    ),
    "LLMBenchLabWorkerStalled": (
        "llmbenchlab_worker_shortfall > 0",
        "2m",
        "critical",
        "alert-worker-stalled",
    ),
    "LLMBenchLabLeaseRecoverySlow": (
        "llmbenchlab_run_expired_lease_oldest_age_seconds > "
        "llmbenchlab_run_recovery_alert_threshold_seconds",
        "1m",
        "warning",
        "alert-lease-recovery-slow",
    ),
}

_DELIVERED_METRICS = {
    "up",
    "llmbenchlab_runs_due_pending",
    "llmbenchlab_audit_events_window",
    "llmbenchlab_governance_scopes_overdrawn",
    "llmbenchlab_queue_configured",
    "llmbenchlab_queue_available",
    "llmbenchlab_worker_shortfall",
    "llmbenchlab_run_expired_lease_oldest_age_seconds",
    "llmbenchlab_run_recovery_alert_threshold_seconds",
}


def test_prometheus_alert_rules_are_the_exact_low_cardinality_contract() -> None:
    document = json.loads(_RULES.read_text(encoding="utf-8"))

    assert set(document) == {"groups"}
    assert len(document["groups"]) == 1
    group = document["groups"][0]
    assert set(group) == {"name", "interval", "rules"}
    assert group["name"] == "llmbenchlab.phase2"
    assert group["interval"] == "30s"
    assert len(group["rules"]) == 8
    assert {rule["alert"] for rule in group["rules"]} == set(_EXPECTED_RULES)

    for rule in group["rules"]:
        assert set(rule) == {"alert", "expr", "for", "labels", "annotations"}
        expression, duration, severity, anchor = _EXPECTED_RULES[rule["alert"]]
        assert rule["expr"] == expression
        assert rule["for"] == duration
        assert rule["labels"] == {
            "severity": severity,
            "component": "llmbenchlab-control-plane",
        }
        assert set(rule["annotations"]) == {
            "summary",
            "description",
            "runbook_url",
            "silence_policy",
        }
        assert rule["annotations"]["summary"]
        assert rule["annotations"]["description"]
        assert "owner" in rule["annotations"]["silence_policy"].lower() or rule["alert"] in {
            "LLMBenchLabGovernanceIntegrityError",
            "LLMBenchLabGovernanceOverdrawn",
        }
        assert rule["annotations"]["runbook_url"] == (
            "https://github.com/CWNU-Open-Source-Community/LLMBenchLab/blob/main/"
            f"docs/OPERATIONS.md#{anchor}"
        )
        assert "rate(" not in expression
        assert "increase(" not in expression

        identifiers = set(re.findall(r"\b(?:up|llmbenchlab_[a-z0-9_]+)\b", expression))
        assert identifiers <= _DELIVERED_METRICS


def test_prometheus_scrape_example_uses_the_fixed_endpoint_and_safe_interval() -> None:
    text = _SCRAPE.read_text(encoding="utf-8")

    assert "job_name: llmbenchlab" in text
    assert "scrape_interval: 60s" in text
    assert "metrics_path: /api/v1/metrics/prometheus" in text
    assert "api:8000" in text
    assert "30s" not in text


def test_prometheus_alert_runbook_targets_exist_exactly_once() -> None:
    operations = _OPERATIONS.read_text(encoding="utf-8")

    for _expression, _duration, _severity, anchor in _EXPECTED_RULES.values():
        assert operations.count(f'<a id="{anchor}"></a>') == 1
