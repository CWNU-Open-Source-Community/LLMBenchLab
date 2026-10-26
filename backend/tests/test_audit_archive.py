"""Canonical audit archive format and filesystem boundary tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import app.governance.audit_archive as audit_archive_module
from app.governance.audit import validate_audit_event_values_for_read
from app.governance.audit_archive import (
    ARCHIVE_EVENT_LIMIT,
    AuditArchiveError,
    build_archive_bytes,
    verify_archive,
    write_archive,
)
from app.models import AuditRetentionClass


def _facts(index: int = 1):
    occurred_at = datetime(2025, 1, index, 12, 0, tzinfo=UTC)
    payload = {"dispatch_count": index}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return validate_audit_event_values_for_read(
        id=f"00000000-0000-0000-0000-{index:012d}",
        event_key=f"archive:run:{index}:claimed",
        event_type="run_claimed",
        payload_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        payload=payload,
        retention_class=AuditRetentionClass.OPERATIONAL,
        occurred_at=occurred_at,
        expires_at=occurred_at + timedelta(days=90),
        correlation_id=f"run-{index}",
        run_id=f"run-{index}",
        model_id=f"model-{index}",
        question_id=None,
        worker_id=f"worker-{index}",
        reservation_id=None,
        attempt=0,
        provider_attempt=None,
        lease_token=index,
        duration_ms=-0.0,
    )


def _settled_facts():
    occurred_at = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    payload = {
        "disposition": "settled_actual",
        "outcome": "succeeded",
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": "1",
        "reconciled": False,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return validate_audit_event_values_for_read(
        id="00000000-0000-0000-0000-000000000099",
        event_key="archive:provider-attempt:settled",
        event_type="provider_attempt_settled",
        payload_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        payload=payload,
        retention_class=AuditRetentionClass.OPERATIONAL,
        occurred_at=occurred_at,
        expires_at=occurred_at + timedelta(days=90),
        correlation_id=None,
        run_id=None,
        model_id=None,
        question_id=None,
        worker_id=None,
        reservation_id=None,
        attempt=None,
        provider_attempt=1,
        lease_token=None,
        duration_ms=None,
    )


def test_archive_round_trip_is_canonical_private_and_no_replace(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    events = (_facts(1), _facts(2))
    result = write_archive(
        path,
        events,
        cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_alembic_head="20260828_0005",
        has_more_eligible=True,
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes().endswith(b"\n")
    assert b'"duration_ms_hex":"0x0.0p+0"' in path.read_bytes()
    verified = verify_archive(path, expected_sha256=result.archive_sha256)
    assert verified.events == events
    assert verified.has_more_eligible is True
    assert verified.content_sha256 == result.content_sha256
    assert verified.archive_sha256 == result.archive_sha256

    with pytest.raises(AuditArchiveError, match="archive_output_exists"):
        write_archive(
            path,
            events,
            cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
            source_alembic_head="20260828_0005",
            has_more_eligible=False,
        )


def test_archive_rejects_broad_permissions_symlink_and_digest_mismatch(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    result = write_archive(
        path,
        (_facts(),),
        cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_alembic_head="20260828_0005",
        has_more_eligible=False,
    )
    os.chmod(path, 0o640)
    with pytest.raises(AuditArchiveError, match="archive_input_permissions_too_broad"):
        verify_archive(path)

    os.chmod(path, 0o600)
    with pytest.raises(AuditArchiveError, match="archive_digest_mismatch"):
        verify_archive(path, expected_sha256="0" * 64)
    assert verify_archive(path, expected_sha256=result.archive_sha256).events

    link = tmp_path / "link.jsonl"
    link.symlink_to(path)
    with pytest.raises(AuditArchiveError, match="archive_input_open_failed"):
        verify_archive(link)


def test_archive_output_rejects_symlink_unowned_or_writable_parent(
    tmp_path,
    monkeypatch,
) -> None:
    private_parent = tmp_path / "private"
    private_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(private_parent, target_is_directory=True)
    with pytest.raises(AuditArchiveError, match="archive_output_parent_invalid"):
        write_archive(
            linked_parent / "audit.jsonl",
            (),
            cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
            source_alembic_head="20260828_0005",
            has_more_eligible=False,
        )

    private_parent.chmod(0o720)
    with pytest.raises(
        AuditArchiveError,
        match="archive_output_parent_permissions_too_broad",
    ):
        write_archive(
            private_parent / "audit.jsonl",
            (),
            cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
            source_alembic_head="20260828_0005",
            has_more_eligible=False,
        )
    private_parent.chmod(0o700)

    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(AuditArchiveError, match="archive_output_parent_not_owned"):
        write_archive(
            private_parent / "audit.jsonl",
            (),
            cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
            source_alembic_head="20260828_0005",
            has_more_eligible=False,
        )


def test_archive_rejects_duplicate_json_keys_and_noncanonical_records(tmp_path) -> None:
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_bytes(
        b'{"record_type":"header","record_type":"header"}\n{"record_type":"manifest"}\n'
    )
    os.chmod(duplicate, 0o600)
    with pytest.raises(AuditArchiveError, match="archive_json_duplicate_key"):
        verify_archive(duplicate)

    noncanonical = tmp_path / "noncanonical.jsonl"
    data, _digest = build_archive_bytes(
        (_facts(),),
        cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_alembic_head="20260828_0005",
        has_more_eligible=False,
    )
    noncanonical.write_bytes(data.replace(b'{"cutoff_at"', b'{ "cutoff_at"', 1))
    os.chmod(noncanonical, 0o600)
    with pytest.raises(AuditArchiveError, match="archive_record_not_canonical"):
        verify_archive(noncanonical)


def test_archive_enforces_event_limit_before_materializing_output() -> None:
    with pytest.raises(AuditArchiveError, match="archive_event_limit_exceeded"):
        build_archive_bytes(
            (_facts(),) * (ARCHIVE_EVENT_LIMIT + 1),
            cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
            source_alembic_head="20260828_0005",
            has_more_eligible=False,
        )


def test_archive_rejects_excess_record_count_before_decoding(tmp_path, monkeypatch) -> None:
    path = tmp_path / "too-many-small-records.jsonl"
    path.write_bytes(b"{}\n" * (ARCHIVE_EVENT_LIMIT + 3))
    path.chmod(0o600)

    monkeypatch.setattr(
        audit_archive_module,
        "_decode_line",
        lambda _line: pytest.fail("over-limit archive reached JSON decoding"),
    )

    with pytest.raises(AuditArchiveError, match="archive_event_limit_exceeded"):
        verify_archive(path)


def test_manifest_counts_reject_boolean_number_confusion(tmp_path) -> None:
    data, _digest = build_archive_bytes(
        (_facts(),),
        cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_alembic_head="20260828_0005",
        has_more_eligible=False,
    )
    lines = data.rstrip(b"\n").split(b"\n")
    manifest = json.loads(lines[-1])
    manifest["event_count"] = True
    lines[-1] = json.dumps(
        manifest,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    path = tmp_path / "bool-count.jsonl"
    path.write_bytes(b"\n".join(lines) + b"\n")
    path.chmod(0o600)
    with pytest.raises(AuditArchiveError, match="archive_manifest_rollup_invalid"):
        verify_archive(path)


def test_archive_rejects_noncanonical_raw_payload_even_when_normalized_hash_matches() -> None:
    canonical = _settled_facts()
    drifted = replace(
        canonical,
        payload={**canonical.payload, "cost_usd": 1},
    )

    with pytest.raises(AuditArchiveError, match="archive_event_integrity_invalid"):
        build_archive_bytes(
            (drifted,),
            cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
            source_alembic_head="20260828_0005",
            has_more_eligible=False,
        )


def test_archive_v1_rejects_unsupported_source_revision_on_write_and_read(tmp_path) -> None:
    with pytest.raises(AuditArchiveError, match="archive_source_revision_unsupported"):
        build_archive_bytes(
            (),
            cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
            source_alembic_head="future_9999",
            has_more_eligible=False,
        )

    fixture = Path(__file__).with_name("fixtures") / "audit-archive-v1-empty.jsonl"
    path = tmp_path / "unsupported-source.jsonl"
    path.write_bytes(fixture.read_bytes().replace(b"20260828_0005", b"future_9999"))
    path.chmod(0o600)
    with pytest.raises(AuditArchiveError, match="archive_source_revision_unsupported"):
        verify_archive(path)


@pytest.mark.parametrize(
    "source_revision",
    ["20260829_0006", "20260830_0007", "20260830_0008"],
)
def test_archive_v1_accepts_compatible_repair_revision(source_revision: str) -> None:
    data, _digest = build_archive_bytes(
        (),
        cutoff_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_alembic_head=source_revision,
        has_more_eligible=False,
    )

    assert f'"source_alembic_head":"{source_revision}"'.encode() in data


def test_frozen_archive_v1_fixture_remains_verifiable(tmp_path) -> None:
    fixture = Path(__file__).with_name("fixtures") / "audit-archive-v1-empty.jsonl"
    path = tmp_path / "archive-v1-empty.jsonl"
    path.write_bytes(fixture.read_bytes())
    path.chmod(0o600)

    verified = verify_archive(
        path,
        expected_sha256="6c0d95198ea8767787e2572b55b3acbfaa3e165a57760c18d8f183254f72fa3d",
    )

    assert verified.source_alembic_head == "20260828_0005"
    assert verified.events == ()
    assert verified.content_sha256 == (
        "a551a37348b9301e39f1c841364649642a4108cec2f3486b77c04c2c714ff02b"
    )

    settled_fixture = Path(__file__).with_name("fixtures") / "audit-archive-v1-settled.jsonl"
    settled_path = tmp_path / "archive-v1-settled.jsonl"
    settled_path.write_bytes(settled_fixture.read_bytes())
    settled_path.chmod(0o600)
    settled = verify_archive(
        settled_path,
        expected_sha256="f3cf9e3484de84cd318295315b3210c66ecf6c66d33076d069a2c3ba2f1e36d8",
    )

    assert len(settled.events) == 1
    assert settled.events[0].event_type == "provider_attempt_settled"
    assert settled.events[0].payload["cost_usd"] == "1"
    assert settled.content_sha256 == (
        "adf5294725e93f311eee1e43cd27a41aa56b401263a565968844066b4a59e616"
    )
