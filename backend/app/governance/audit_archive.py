"""Canonical, bounded audit-retention archive format.

The archive is an internal sensitive operations artifact.  Its hashes detect
accidental or uncoordinated changes; they are not signatures and do not make a
regular file immutable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.governance.audit import (
    AuditEventReadFacts,
    AuditIntegrityError,
    validate_audit_archive_v1_event_values_for_read,
)
from app.models import AuditRetentionClass

ARCHIVE_SCHEMA = "llmbenchlab-audit-archive-v1"
ARCHIVE_V1_COMPATIBLE_ALEMBIC_HEADS = frozenset({"20260828_0005"})
ARCHIVE_V1_RETENTION_VALUES = ("operational", "security")
ARCHIVE_EVENT_LIMIT = 10_000
ARCHIVE_FILE_LIMIT_BYTES = 128 * 1024 * 1024
ARCHIVE_LINE_LIMIT_BYTES = 64 * 1024
_CONTENT_HASH_DOMAIN = b"LLMBenchLab audit archive content v1\x00"
_SHA256 = re.compile(r"[a-f0-9]{64}")
_ALEMBIC_REVISION = re.compile(r"[A-Za-z0-9_]{1,64}")

_HEADER_KEYS = frozenset(
    {
        "record_type",
        "schema",
        "cutoff_at",
        "source_alembic_head",
        "event_limit",
    }
)
_EVENT_KEYS = frozenset(
    {
        "record_type",
        "id",
        "event_key",
        "event_type",
        "payload_hash",
        "payload",
        "retention_class",
        "occurred_at",
        "expires_at",
        "correlation_id",
        "run_id",
        "model_id",
        "question_id",
        "worker_id",
        "reservation_id",
        "attempt",
        "provider_attempt",
        "lease_token",
        "duration_ms_hex",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "record_type",
        "schema",
        "cutoff_at",
        "source_alembic_head",
        "event_count",
        "event_type_counts",
        "retention_class_counts",
        "occurred_at_min",
        "occurred_at_max",
        "expires_at_min",
        "expires_at_max",
        "has_more_eligible",
        "content_sha256",
    }
)


class AuditArchiveError(RuntimeError):
    """A fixed-code archive format, path, or integrity failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class VerifiedAuditArchive:
    """Strictly parsed archive held in memory after single-descriptor verification."""

    cutoff_at: datetime
    source_alembic_head: str
    events: tuple[AuditEventReadFacts, ...]
    has_more_eligible: bool
    content_sha256: str
    archive_sha256: str


@dataclass(frozen=True)
class WrittenAuditArchive:
    """Archive output summary safe for fixed CLI reporting."""

    event_count: int
    has_more_eligible: bool
    content_sha256: str
    archive_sha256: str


def _canonical_json(value: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise AuditArchiveError("archive_value_not_canonical_json") from None
    if len(encoded) > ARCHIVE_LINE_LIMIT_BYTES:
        raise AuditArchiveError("archive_line_limit_exceeded")
    return encoded


def _format_timestamp(value: datetime) -> str:
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise AuditArchiveError("archive_timestamp_invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise AuditArchiveError("archive_timestamp_invalid") from None
    if _format_timestamp(parsed) != value:
        raise AuditArchiveError("archive_timestamp_not_canonical")
    return parsed


def _duration_to_hex(value: float | None) -> str | None:
    if value is None:
        return None
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise AuditArchiveError("archive_duration_invalid")
    if normalized == 0:
        normalized = 0.0
    return normalized.hex()


def _duration_from_hex(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuditArchiveError("archive_duration_invalid")
    try:
        parsed = float.fromhex(value)
    except ValueError:
        raise AuditArchiveError("archive_duration_invalid") from None
    canonical = _duration_to_hex(parsed)
    if canonical != value:
        raise AuditArchiveError("archive_duration_not_canonical")
    return parsed


def _event_record(event: AuditEventReadFacts) -> dict[str, Any]:
    return {
        "record_type": "audit_event",
        "id": event.id,
        "event_key": event.event_key,
        "event_type": event.event_type,
        "payload_hash": event.payload_hash,
        "payload": dict(event.payload),
        "retention_class": event.retention_class.value,
        "occurred_at": _format_timestamp(event.occurred_at),
        "expires_at": _format_timestamp(event.expires_at),
        "correlation_id": event.correlation_id,
        "run_id": event.run_id,
        "model_id": event.model_id,
        "question_id": event.question_id,
        "worker_id": event.worker_id,
        "reservation_id": event.reservation_id,
        "attempt": event.attempt,
        "provider_attempt": event.provider_attempt,
        "lease_token": event.lease_token,
        "duration_ms_hex": _duration_to_hex(event.duration_ms),
    }


def _validated_facts(event: AuditEventReadFacts) -> AuditEventReadFacts:
    try:
        return validate_audit_archive_v1_event_values_for_read(
            id=event.id,
            event_key=event.event_key,
            event_type=event.event_type,
            payload_hash=event.payload_hash,
            payload=event.payload,
            retention_class=event.retention_class,
            occurred_at=event.occurred_at,
            expires_at=event.expires_at,
            correlation_id=event.correlation_id,
            run_id=event.run_id,
            model_id=event.model_id,
            question_id=event.question_id,
            worker_id=event.worker_id,
            reservation_id=event.reservation_id,
            attempt=event.attempt,
            provider_attempt=event.provider_attempt,
            lease_token=event.lease_token,
            duration_ms=event.duration_ms,
        )
    except (AttributeError, AuditIntegrityError):
        raise AuditArchiveError("archive_event_integrity_invalid") from None


def _content_digest(lines: list[bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(_CONTENT_HASH_DOMAIN)
    for line in lines:
        digest.update(len(line).to_bytes(8, "big"))
        digest.update(line)
    return digest.hexdigest()


def _validate_source_head(value: object) -> str:
    if not isinstance(value, str) or not _ALEMBIC_REVISION.fullmatch(value):
        raise AuditArchiveError("archive_source_revision_invalid")
    if value not in ARCHIVE_V1_COMPATIBLE_ALEMBIC_HEADS:
        raise AuditArchiveError("archive_source_revision_unsupported")
    return value


def build_archive_bytes(
    events: tuple[AuditEventReadFacts, ...],
    *,
    cutoff_at: datetime,
    source_alembic_head: str,
    has_more_eligible: bool,
) -> tuple[bytes, str]:
    """Build canonical archive bytes and the non-self-referential content digest."""

    if len(events) > ARCHIVE_EVENT_LIMIT:
        raise AuditArchiveError("archive_event_limit_exceeded")
    if not isinstance(has_more_eligible, bool):
        raise AuditArchiveError("archive_has_more_invalid")
    source_alembic_head = _validate_source_head(source_alembic_head)
    try:
        cutoff = _parse_timestamp(_format_timestamp(cutoff_at))
    except (AttributeError, OverflowError, ValueError):
        raise AuditArchiveError("archive_cutoff_invalid") from None
    events = tuple(_validated_facts(event) for event in events)

    previous_order: tuple[datetime, str] | None = None
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for event in events:
        order = (event.expires_at, event.id)
        if previous_order is not None and order <= previous_order:
            raise AuditArchiveError("archive_events_not_strictly_ordered")
        if event.id in seen_ids or event.event_key in seen_keys:
            raise AuditArchiveError("archive_event_identity_duplicated")
        if event.expires_at >= cutoff:
            raise AuditArchiveError("archive_event_not_eligible")
        previous_order = order
        seen_ids.add(event.id)
        seen_keys.add(event.event_key)

    header = {
        "record_type": "header",
        "schema": ARCHIVE_SCHEMA,
        "cutoff_at": _format_timestamp(cutoff),
        "source_alembic_head": source_alembic_head,
        "event_limit": ARCHIVE_EVENT_LIMIT,
    }
    content_lines = [_canonical_json(header), *(_canonical_json(_event_record(e)) for e in events)]
    content_sha256 = _content_digest(content_lines)

    event_type_counts = dict(sorted(Counter(event.event_type for event in events).items()))
    retention_counts = {
        retention: sum(event.retention_class.value == retention for event in events)
        for retention in ARCHIVE_V1_RETENTION_VALUES
    }
    occurred_values = [event.occurred_at for event in events]
    expiry_values = [event.expires_at for event in events]
    manifest = {
        "record_type": "manifest",
        "schema": ARCHIVE_SCHEMA,
        "cutoff_at": _format_timestamp(cutoff),
        "source_alembic_head": source_alembic_head,
        "event_count": len(events),
        "event_type_counts": event_type_counts,
        "retention_class_counts": retention_counts,
        "occurred_at_min": _format_timestamp(min(occurred_values)) if events else None,
        "occurred_at_max": _format_timestamp(max(occurred_values)) if events else None,
        "expires_at_min": _format_timestamp(min(expiry_values)) if events else None,
        "expires_at_max": _format_timestamp(max(expiry_values)) if events else None,
        "has_more_eligible": has_more_eligible,
        "content_sha256": content_sha256,
    }
    all_lines = [*content_lines, _canonical_json(manifest)]
    data = b"\n".join(all_lines) + b"\n"
    if len(data) > ARCHIVE_FILE_LIMIT_BYTES:
        raise AuditArchiveError("archive_file_limit_exceeded")
    return data, content_sha256


def _write_all(file_descriptor: int, data: bytes) -> None:
    position = 0
    while position < len(data):
        written = os.write(file_descriptor, data[position:])
        if written <= 0:
            raise AuditArchiveError("archive_output_write_failed")
        position += written


def write_archive_no_replace(path: Path, data: bytes) -> str:
    """Install one 0600 archive atomically without replacing any target."""

    output = Path(path)
    parent = output.parent
    if not output.name:
        raise AuditArchiveError("archive_output_parent_invalid")
    if len(data) > ARCHIVE_FILE_LIMIT_BYTES:
        raise AuditArchiveError("archive_file_limit_exceeded")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    directory_fd: int | None = None
    temporary_name = f".{output.name}.tmp-{secrets.token_hex(12)}"
    file_descriptor: int | None = None
    installed = False
    completed = False
    try:
        try:
            directory_fd = os.open(parent, directory_flags)
        except OSError:
            raise AuditArchiveError("archive_output_parent_invalid") from None
        parent_details = os.fstat(directory_fd)
        if not stat.S_ISDIR(parent_details.st_mode) or parent_details.st_uid != os.geteuid():
            raise AuditArchiveError("archive_output_parent_not_owned")
        if stat.S_IMODE(parent_details.st_mode) & 0o022:
            raise AuditArchiveError("archive_output_parent_permissions_too_broad")
        file_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(file_descriptor, 0o600)
        _write_all(file_descriptor, data)
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = None
        os.link(
            temporary_name,
            output.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        installed = True
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        completed = True
    except FileExistsError:
        raise AuditArchiveError("archive_output_exists") from None
    except AuditArchiveError:
        raise
    except OSError:
        raise AuditArchiveError("archive_output_filesystem_error") from None
    finally:
        if file_descriptor is not None:
            with suppress(OSError):
                os.close(file_descriptor)
        if directory_fd is not None:
            with suppress(OSError):
                os.unlink(temporary_name, dir_fd=directory_fd)
            if installed and not completed:
                # The target can only be ours after a successful no-replace
                # link.  Remove it when the directory durability step fails so
                # a failed command does not leave an apparent success artifact.
                with suppress(OSError):
                    os.unlink(output.name, dir_fd=directory_fd)
                with suppress(OSError):
                    os.fsync(directory_fd)
            with suppress(OSError):
                os.close(directory_fd)
    return hashlib.sha256(data).hexdigest()


def write_archive(
    path: Path,
    events: tuple[AuditEventReadFacts, ...],
    *,
    cutoff_at: datetime,
    source_alembic_head: str,
    has_more_eligible: bool,
) -> WrittenAuditArchive:
    data, content_sha256 = build_archive_bytes(
        events,
        cutoff_at=cutoff_at,
        source_alembic_head=source_alembic_head,
        has_more_eligible=has_more_eligible,
    )
    archive_sha256 = write_archive_no_replace(path, data)
    return WrittenAuditArchive(
        event_count=len(events),
        has_more_eligible=has_more_eligible,
        content_sha256=content_sha256,
        archive_sha256=archive_sha256,
    )


def _reject_json_constant(_value: str) -> None:
    raise AuditArchiveError("archive_json_non_finite")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuditArchiveError("archive_json_duplicate_key")
        result[key] = value
    return result


def _decode_line(line: bytes) -> dict[str, Any]:
    if not line or len(line) > ARCHIVE_LINE_LIMIT_BYTES:
        raise AuditArchiveError("archive_line_invalid")
    try:
        text = line.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except AuditArchiveError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AuditArchiveError("archive_json_invalid") from None
    if not isinstance(value, dict):
        raise AuditArchiveError("archive_record_not_object")
    if _canonical_json(value) != line:
        raise AuditArchiveError("archive_record_not_canonical")
    return value


def _require_exact_keys(value: dict[str, Any], keys: frozenset[str], code: str) -> None:
    if frozenset(value) != keys:
        raise AuditArchiveError(code)


def _parse_event(value: dict[str, Any]) -> AuditEventReadFacts:
    _require_exact_keys(value, _EVENT_KEYS, "archive_event_schema_invalid")
    if value["record_type"] != "audit_event":
        raise AuditArchiveError("archive_event_type_invalid")
    retention_value = value["retention_class"]
    if not isinstance(retention_value, str) or retention_value not in ARCHIVE_V1_RETENTION_VALUES:
        raise AuditArchiveError("archive_retention_class_invalid") from None
    try:
        retention = AuditRetentionClass(retention_value)
    except ValueError:
        raise AuditArchiveError("archive_retention_class_invalid") from None
    try:
        return validate_audit_archive_v1_event_values_for_read(
            id=value["id"],
            event_key=value["event_key"],
            event_type=value["event_type"],
            payload_hash=value["payload_hash"],
            payload=value["payload"],
            retention_class=retention,
            occurred_at=_parse_timestamp(value["occurred_at"]),
            expires_at=_parse_timestamp(value["expires_at"]),
            correlation_id=value["correlation_id"],
            run_id=value["run_id"],
            model_id=value["model_id"],
            question_id=value["question_id"],
            worker_id=value["worker_id"],
            reservation_id=value["reservation_id"],
            attempt=value["attempt"],
            provider_attempt=value["provider_attempt"],
            lease_token=value["lease_token"],
            duration_ms=_duration_from_hex(value["duration_ms_hex"]),
        )
    except AuditIntegrityError:
        raise AuditArchiveError("archive_event_integrity_invalid") from None


def _read_owned_private_file(path: Path) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(Path(path), os.O_RDONLY | nofollow | nonblock)
    except OSError:
        raise AuditArchiveError("archive_input_open_failed") from None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise AuditArchiveError("archive_input_not_regular")
        if details.st_uid != os.geteuid():
            raise AuditArchiveError("archive_input_wrong_owner")
        if stat.S_IMODE(details.st_mode) & ~0o600:
            raise AuditArchiveError("archive_input_permissions_too_broad")
        if details.st_size > ARCHIVE_FILE_LIMIT_BYTES:
            raise AuditArchiveError("archive_file_limit_exceeded")
        chunks: list[bytes] = []
        remaining = ARCHIVE_FILE_LIMIT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > ARCHIVE_FILE_LIMIT_BYTES:
            raise AuditArchiveError("archive_file_limit_exceeded")
        return data
    except OSError:
        raise AuditArchiveError("archive_input_read_failed") from None
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _validate_digest(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise AuditArchiveError(code)
    return value


def _validate_count_mapping(value: object) -> None:
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        for key, count in value.items()
    ):
        raise AuditArchiveError("archive_manifest_rollup_invalid")


def verify_archive(
    path: Path,
    *,
    expected_sha256: str | None = None,
) -> VerifiedAuditArchive:
    """Strictly verify one archive from a single opened file descriptor."""

    data = _read_owned_private_file(path)
    archive_sha256 = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None:
        expected = _validate_digest(expected_sha256, code="archive_expected_digest_invalid")
        if not hmac.compare_digest(archive_sha256, expected):
            raise AuditArchiveError("archive_digest_mismatch")
    if not data or not data.endswith(b"\n") or data.endswith(b"\n\n"):
        raise AuditArchiveError("archive_line_termination_invalid")
    # One header, at most ARCHIVE_EVENT_LIMIT events, and one manifest are the
    # complete v1 record set. Enforce that bound before splitting or decoding
    # attacker-controlled small lines into Python objects.
    if data.count(b"\n") > ARCHIVE_EVENT_LIMIT + 2:
        raise AuditArchiveError("archive_event_limit_exceeded")
    raw_lines = data[:-1].split(b"\n")
    if len(raw_lines) < 2 or any(not line for line in raw_lines):
        raise AuditArchiveError("archive_record_sequence_invalid")

    values = [_decode_line(line) for line in raw_lines]
    header = values[0]
    manifest = values[-1]
    _require_exact_keys(header, _HEADER_KEYS, "archive_header_schema_invalid")
    _require_exact_keys(manifest, _MANIFEST_KEYS, "archive_manifest_schema_invalid")
    if header["record_type"] != "header" or manifest["record_type"] != "manifest":
        raise AuditArchiveError("archive_record_sequence_invalid")
    if header["schema"] != ARCHIVE_SCHEMA or manifest["schema"] != ARCHIVE_SCHEMA:
        raise AuditArchiveError("archive_schema_unsupported")
    if header["event_limit"] != ARCHIVE_EVENT_LIMIT:
        raise AuditArchiveError("archive_event_limit_invalid")
    cutoff_at = _parse_timestamp(header["cutoff_at"])
    source_head = _validate_source_head(header["source_alembic_head"])
    if manifest["cutoff_at"] != header["cutoff_at"]:
        raise AuditArchiveError("archive_manifest_header_mismatch")
    if manifest["source_alembic_head"] != source_head:
        raise AuditArchiveError("archive_manifest_header_mismatch")
    if (
        isinstance(manifest["event_count"], bool)
        or not isinstance(manifest["event_count"], int)
        or not 0 <= manifest["event_count"] <= ARCHIVE_EVENT_LIMIT
    ):
        raise AuditArchiveError("archive_manifest_rollup_invalid")
    _validate_count_mapping(manifest["event_type_counts"])
    _validate_count_mapping(manifest["retention_class_counts"])

    event_values = values[1:-1]
    if len(event_values) > ARCHIVE_EVENT_LIMIT:
        raise AuditArchiveError("archive_event_limit_exceeded")
    events = tuple(_parse_event(value) for value in event_values)
    previous_order: tuple[datetime, str] | None = None
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for event in events:
        order = (event.expires_at, event.id)
        if previous_order is not None and order <= previous_order:
            raise AuditArchiveError("archive_events_not_strictly_ordered")
        if event.id in seen_ids or event.event_key in seen_keys:
            raise AuditArchiveError("archive_event_identity_duplicated")
        if event.expires_at >= cutoff_at:
            raise AuditArchiveError("archive_event_not_eligible")
        previous_order = order
        seen_ids.add(event.id)
        seen_keys.add(event.event_key)

    expected_content = _content_digest(raw_lines[:-1])
    content_sha256 = _validate_digest(
        manifest["content_sha256"], code="archive_content_digest_invalid"
    )
    if not hmac.compare_digest(expected_content, content_sha256):
        raise AuditArchiveError("archive_content_digest_mismatch")

    expected_type_counts = dict(sorted(Counter(event.event_type for event in events).items()))
    expected_retention_counts = {
        retention: sum(event.retention_class.value == retention for event in events)
        for retention in ARCHIVE_V1_RETENTION_VALUES
    }
    occurred = [event.occurred_at for event in events]
    expires = [event.expires_at for event in events]
    expected_rollup = {
        "event_count": len(events),
        "event_type_counts": expected_type_counts,
        "retention_class_counts": expected_retention_counts,
        "occurred_at_min": _format_timestamp(min(occurred)) if events else None,
        "occurred_at_max": _format_timestamp(max(occurred)) if events else None,
        "expires_at_min": _format_timestamp(min(expires)) if events else None,
        "expires_at_max": _format_timestamp(max(expires)) if events else None,
    }
    for key, expected_value in expected_rollup.items():
        if manifest[key] != expected_value:
            raise AuditArchiveError("archive_manifest_rollup_mismatch")
    if not isinstance(manifest["has_more_eligible"], bool):
        raise AuditArchiveError("archive_has_more_invalid")

    return VerifiedAuditArchive(
        cutoff_at=cutoff_at,
        source_alembic_head=source_head,
        events=events,
        has_more_eligible=manifest["has_more_eligible"],
        content_sha256=content_sha256,
        archive_sha256=archive_sha256,
    )


def confirm_archive_digest(archive: VerifiedAuditArchive, confirmation: str) -> None:
    expected = _validate_digest(confirmation, code="archive_confirmation_digest_invalid")
    if not hmac.compare_digest(archive.archive_sha256, expected):
        raise AuditArchiveError("archive_confirmation_digest_mismatch")
