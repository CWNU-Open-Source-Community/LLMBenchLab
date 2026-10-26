#!/usr/bin/env python3
"""Repeatable real-Compose acceptance for the Phase 2 reliability slice.

The harness intentionally uses only Python's standard library.  It creates a
unique Compose project, exposes only random loopback API/frontend ports, runs
offline Mock evaluations, records sanitized JSON evidence under an ignored
directory, and always removes the project's containers, network, and volumes.

Run from any directory with::

    python3 scripts/phase2_acceptance.py

Use ``--self-check-only`` to validate Docker, Compose, isolation, and cleanup
guards without creating containers.
"""

import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set

EVIDENCE_SCHEMA = "llmbenchlab-phase2-acceptance-evidence-v1"
PROTOCOL_VERSION = "llmbenchlab-protocol-v1"
PRE_GOVERNANCE_REVISION = "20260827_0003"
GOVERNANCE_REVISION = "20260827_0004"
WORKER_PROGRESS_REVISION = "20260828_0005"
DATABASE_HEAD_REVISION = "20260830_0008"
TASK_MESSAGE_VERSION = "llmbenchlab-run-task-v1"
TASK_STREAM = "llmbenchlab:runs:v1"
TASK_GROUP = "llmbenchlab-workers-v1"
PROJECT_PATTERN = re.compile(r"^llmbenchlab-p2-[0-9a-f]{12}$")
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
DATABASE_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<head>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,6}))?"
    r"(?P<offset>[+-]\d{2}:\d{2})?$"
)
LOCAL_PASSWORD = "llmbenchlab-local-only"
DEFAULT_ARTIFACTS_ROOT = Path(".pytest_cache/artifacts/phase2-acceptance")

# Executed inside the already-isolated API container.  The helper uses the same
# production repositories as a Worker to create a coherent attempt boundary,
# then expires only the database lease.  This is deliberately a deterministic
# database seam injection, not a claim that a SIGKILL landed in a sub-millisecond
# code window.
DB_SEAM_HELPER_SOURCE = r"""
import json
import sys
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text

from app.db.session import SessionLocal
from app.governance import GovernanceRepository, provider_scope_key
from app.models import EvaluationResponse, EvaluationRun, Question
from app.provider_attempts import ProviderAttemptDisposition, ProviderAttemptOutcome
from app.runners.run_leases import ResponseDisposition, RunLeaseRepository


mode = sys.argv[1]
run_id = sys.argv[2]
baseline_run_id = None if len(sys.argv) < 4 or sys.argv[3] == "-" else sys.argv[3]
if mode not in {"reserved", "send_started", "response_committed"}:
    raise RuntimeError("unsupported database seam mode")
if mode == "response_committed" and baseline_run_id is None:
    raise RuntimeError("response_committed requires a baseline Run")

owner = "acceptance-db-seam:" + mode
leases = RunLeaseRepository(SessionLocal, lease_for=timedelta(seconds=30))
governance = GovernanceRepository(SessionLocal)
lease = leases.claim(run_id, owner=owner)
if lease is None:
    raise RuntimeError("database seam Run was not claimable")

with SessionLocal() as session:
    run = session.get(EvaluationRun, run_id)
    if run is None:
        raise RuntimeError("database seam Run disappeared")
    question = session.scalar(
        select(Question)
        .where(Question.benchmark_id == run.benchmark_id)
        .order_by(Question.position, Question.id)
        .limit(1)
    )
    if question is None:
        raise RuntimeError("database seam question was unavailable")
    model_id = run.model_id
    question_id = question.id

context = governance.question_context(
    run_id=run_id,
    question_id=question_id,
    model_id=model_id,
    provider_scope=provider_scope_key("mock", None),
    lease_owner=owner,
    lease_token=lease.token,
    reserved_output_tokens=64,
    reserved_cost_usd=Decimal("0"),
)
permit = governance.reserve(context, provider_attempt=1, lease_owner=owner)
response_id = None
if mode in {"send_started", "response_committed"}:
    governance.mark_send_started(permit, lease_owner=owner)
if mode == "response_committed":
    governance.finish(
        permit,
        disposition=ProviderAttemptDisposition.SETTLED_ACTUAL,
        outcome=ProviderAttemptOutcome.SUCCEEDED,
        input_tokens=8,
        output_tokens=2,
        actual_cost_usd=Decimal("0"),
    )
    with SessionLocal() as session:
        baseline = session.scalar(
            select(EvaluationResponse).where(
                EvaluationResponse.run_id == baseline_run_id,
                EvaluationResponse.question_id == question_id,
            )
        )
        if baseline is None:
            raise RuntimeError("baseline Response for database seam was unavailable")
        response_id = str(uuid4())
        response = EvaluationResponse(
            id=response_id,
            run_id=run_id,
            question_id=question_id,
            raw_response=baseline.raw_response,
            parsed_answer=baseline.parsed_answer,
            reference_answer_snapshot=baseline.reference_answer_snapshot,
            score=baseline.score,
            evaluator_name=baseline.evaluator_name,
            latency_ms=baseline.latency_ms,
            input_tokens=baseline.input_tokens,
            output_tokens=baseline.output_tokens,
            estimated_cost=baseline.estimated_cost,
            provider_request_id=baseline.provider_request_id,
            returned_model=baseline.returned_model,
            system_fingerprint=baseline.system_fingerprint,
            finish_reason=baseline.finish_reason,
            http_attempt_count=baseline.http_attempt_count,
            error_type=baseline.error_type,
            error_message=baseline.error_message,
        )
    disposition = leases.persist_response(lease, response)
    if disposition != ResponseDisposition.INSERTED:
        raise RuntimeError("database seam Response did not commit exactly once")

with SessionLocal() as session, session.begin():
    session.execute(
        text(
            "UPDATE evaluation_runs "
            "SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second', "
            "heartbeat_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
            "WHERE id = :run_id"
        ),
        {"run_id": run_id},
    )
    session.execute(
        text(
            "UPDATE provider_call_reservations "
            "SET lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
            "WHERE id = :reservation_id "
            "AND state IN ('reserved', 'send_started')"
        ),
        {"reservation_id": permit.reservation_id},
    )

print(
    json.dumps(
        {
            "fault_method": "deterministic_database_seam_injection",
            "sigkill_used": False,
            "mode": mode,
            "run_id": run_id,
            "lease_owner": owner,
            "lease_token": lease.token,
            "question_id": question_id,
            "reservation_id": permit.reservation_id,
            "execution_generation": context.execution_generation,
            "provider_attempt": permit.provider_attempt,
            "response_id": response_id,
        },
        sort_keys=True,
    )
)
"""

RUN_SNAPSHOT_FIELDS = (
    "id",
    "status",
    "protocol_version",
    "total_questions",
    "completed_questions",
    "correct_questions",
    "error_questions",
    "score",
    "completion_rate",
    "answered_accuracy",
    "average_latency_ms",
    "input_tokens",
    "output_tokens",
    "estimated_cost",
    "cancellation_requested",
    "attempt_count",
    "max_attempts",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "heartbeat_at",
    "next_attempt_at",
    "last_enqueued_at",
    "last_error",
    "dead_lettered_at",
    "started_at",
    "finished_at",
    "error_message",
)

RESPONSE_SNAPSHOT_FIELDS = (
    "id",
    "question_id",
    "question_external_id",
    "question_type",
    "raw_response",
    "parsed_answer",
    "reference_answer_snapshot",
    "score",
    "evaluator_name",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "estimated_cost",
    "error_type",
    "error_message",
)


class AcceptanceFailure(RuntimeError):
    """A failed Phase 2 acceptance invariant."""


class AcceptanceInterrupted(RuntimeError):
    """Raised on SIGTERM so normal cleanup can still run."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def parse_datetime(value: str) -> dt.datetime:
    normalized = value.strip()
    normalized = normalized[:-1] + "+00:00" if normalized.endswith("Z") else normalized
    match = DATABASE_TIMESTAMP_PATTERN.fullmatch(normalized)
    if match is not None:
        fraction = match.group("fraction")
        normalized = match.group("head")
        if fraction is not None:
            normalized += "." + fraction.ljust(6, "0")
        normalized += match.group("offset") or ""
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def conservative_settlement_matches_reserved_bounds(
    reservation: dict[str, Any],
) -> bool:
    """Return whether conservative settlement copied every configured bound."""

    return all(
        reservation[f"actual_{dimension}"] == reservation[f"reserved_{dimension}"]
        for dimension in ("input_tokens", "output_tokens", "cost_usd")
    )


def redact_text(value: str) -> str:
    redacted = value.replace(LOCAL_PASSWORD, "<redacted>")
    redacted = re.sub(
        r"(?i)(postgres(?:ql)?(?:\+psycopg)?|redis)://[^\s/@:]+:[^\s/@]+@",
        r"\1://<redacted>@",
        redacted,
    )
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "<redacted-api-key>", redacted)
    redacted = re.sub(
        r'(?i)(authorization["\']?\s*[:=]\s*["\']?)(bearer\s+)?[^\s,"\']+',
        r"\1<redacted>",
        redacted,
    )
    return redacted


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {
                "authorization",
                "password",
                "database_url",
                "redis_url",
                "api_key",
                "secret",
            }:
                sanitized[str(key)] = "<redacted>"
            else:
                sanitized[str(key)] = sanitize(item)
        return sanitized
    return value


class Phase2Acceptance:
    """Own one isolated Compose project for the complete acceptance run."""

    def __init__(self, repository_root: Path, artifacts_root: Path) -> None:
        self.root = repository_root.resolve()
        self.compose_file = self.root / "compose.yaml"
        suffix = uuid.uuid4().hex[:12]
        self.project = "llmbenchlab-p2-" + suffix
        if not PROJECT_PATTERN.fullmatch(self.project):
            raise AcceptanceFailure("generated Compose project name failed its safety guard")

        self.api_port = allocate_loopback_port()
        self.frontend_port = allocate_loopback_port()
        while self.frontend_port == self.api_port:
            self.frontend_port = allocate_loopback_port()
        self.api_base = "http://127.0.0.1:{}/api/v1".format(self.api_port)
        self.frontend_base = "http://127.0.0.1:{}".format(self.frontend_port)

        resolved_artifacts_root = (
            artifacts_root if artifacts_root.is_absolute() else self.root / artifacts_root
        ).resolve()
        try:
            resolved_artifacts_root.relative_to(self.root)
        except ValueError as exc:
            raise AcceptanceFailure("artifacts root must remain inside the repository") from exc
        self.artifact_dir = resolved_artifacts_root / self.project
        self.evidence_path = self.artifact_dir / "evidence.json"

        self._credential_secret_dir = tempfile.TemporaryDirectory(
            prefix="llmbenchlab-p2-credential-"
        )
        credential_keys_path = Path(self._credential_secret_dir.name) / "keys.json"
        credential_keys_path.write_text(
            json.dumps(
                {
                    "active_key_id": "acceptance-v1",
                    "keys": {
                        "acceptance-v1": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(
                            "ascii"
                        )
                    },
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        credential_keys_path.chmod(0o600)

        self.env = os.environ.copy()
        for inherited_key in (
            "COMPOSE_FILE",
            "COMPOSE_PROJECT_NAME",
            "COMPOSE_PROFILES",
            "OPENAI_API_KEY",
            "LLMBENCHLAB_DEMO_API_KEY",
        ):
            self.env.pop(inherited_key, None)
        self.env.update(
            {
                "API_PORT": str(self.api_port),
                "FRONTEND_PORT": str(self.frontend_port),
                "LLMBENCHLAB_IMAGE_TAG": "p2-" + suffix,
                "LLMBENCHLAB_COMPOSE_CREDENTIAL_KEYS_FILE": str(credential_keys_path),
                "LLMBENCHLAB_COMPOSE_DATABASE_URL": (
                    "postgresql+psycopg://llmbenchlab:{}@postgres:5432/"
                    "llmbenchlab?connect_timeout=3"
                ).format(LOCAL_PASSWORD),
                "LLMBENCHLAB_COMPOSE_REDIS_URL": "redis://redis:6379/0",
                "LLMBENCHLAB_COMPOSE_WORKER_LEASE_SECONDS": "6",
                "LLMBENCHLAB_COMPOSE_WORKER_HEARTBEAT_SECONDS": "2",
                "LLMBENCHLAB_COMPOSE_WORKER_POLL_SECONDS": "0.15",
                "LLMBENCHLAB_COMPOSE_WORKER_SHUTDOWN_GRACE_SECONDS": "1",
                "LLMBENCHLAB_COMPOSE_WORKER_EXPECTED_PROCESSES": "2",
                "LLMBENCHLAB_COMPOSE_MOCK_GENERATION_DELAY_SECONDS": "0.35",
                "LLMBENCHLAB_COMPOSE_REDIS_OPERATION_TIMEOUT_SECONDS": "0.75",
                "LLMBENCHLAB_COMPOSE_DATABASE_POOL_TIMEOUT_SECONDS": "2",
                "LLMBENCHLAB_COMPOSE_READINESS_DATABASE_TIMEOUT_SECONDS": "2",
                "LOG_LEVEL": "INFO",
                "COMPOSE_ANSI": "never",
                "BUILDKIT_PROGRESS": "plain",
                "NO_COLOR": "1",
            }
        )

        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.stack_touched = False
        self.model_id: Optional[str] = None
        self.benchmark_id: Optional[str] = None
        self.baseline_run_id: Optional[str] = None
        self.evidence: Dict[str, Any] = {
            "schema_version": EVIDENCE_SCHEMA,
            "status": "initializing",
            "started_at": utc_now(),
            "finished_at": None,
            "project_name": self.project,
            "artifacts": str(self.evidence_path.relative_to(self.root)),
            "offline_only": True,
            "ports": {
                "api_loopback": self.api_port,
                "frontend_loopback": self.frontend_port,
            },
            "timing": {
                "lease_seconds": 6,
                "heartbeat_seconds": 2,
                "worker_poll_seconds": 0.15,
                "mock_generation_delay_seconds": 0.35,
            },
            "self_review": {},
            "topology": {},
            "scenarios": [],
            "commands": [],
            "diagnostics": {},
            "cleanup": {},
            "failure": None,
        }

    def compose_args(self, *arguments: str) -> List[str]:
        return [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            "-p",
            self.project,
            *arguments,
        ]

    def run_command(
        self,
        arguments: Sequence[str],
        timeout: float = 120,
        check: bool = True,
        record: bool = True,
        max_recorded_chars: int = 12000,
    ) -> subprocess.CompletedProcess:
        started = time.monotonic()
        command = [str(item) for item in arguments]
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.root),
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            if record:
                self.evidence["commands"].append(
                    {
                        "at": utc_now(),
                        "command": sanitize(command),
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "returncode": None,
                        "error": "timeout",
                    }
                )
            raise AcceptanceFailure(
                "command exceeded {:.1f}s: {}".format(
                    timeout,
                    redact_text(" ".join(command)),
                )
            ) from exc

        duration = round(time.monotonic() - started, 3)
        if record:
            self.evidence["commands"].append(
                {
                    "at": utc_now(),
                    "command": sanitize(command),
                    "duration_seconds": duration,
                    "returncode": completed.returncode,
                    "stdout_tail": redact_text(completed.stdout[-max_recorded_chars:]),
                    "stderr_tail": redact_text(completed.stderr[-max_recorded_chars:]),
                }
            )
        if check and completed.returncode != 0:
            detail = redact_text((completed.stderr or completed.stdout)[-4000:])
            raise AcceptanceFailure(
                "command failed with {}: {}\n{}".format(
                    completed.returncode,
                    redact_text(" ".join(command)),
                    detail,
                )
            )
        return completed

    def compose(
        self,
        *arguments: str,
        timeout: float = 120,
        check: bool = True,
        record: bool = True,
        max_recorded_chars: int = 12000,
    ) -> subprocess.CompletedProcess:
        return self.run_command(
            self.compose_args(*arguments),
            timeout=timeout,
            check=check,
            record=record,
            max_recorded_chars=max_recorded_chars,
        )

    def require(self, condition: bool, message: str, detail: Any = None) -> None:
        if condition:
            return
        if detail is None:
            raise AcceptanceFailure(message)
        raise AcceptanceFailure("{}: {}".format(message, sanitize(detail)))

    def write_evidence(self) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.evidence_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(sanitize(self.evidence), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.evidence_path)

    @contextlib.contextmanager
    def scenario(self, name: str) -> Iterator[Dict[str, Any]]:
        entry: Dict[str, Any] = {
            "name": name,
            "status": "running",
            "started_at": utc_now(),
            "finished_at": None,
        }
        self.evidence["scenarios"].append(entry)
        self.write_evidence()
        try:
            yield entry
        except BaseException as exc:
            entry["status"] = "failed"
            entry["error"] = redact_text(str(exc))
            raise
        else:
            entry["status"] = "passed"
        finally:
            entry["finished_at"] = utc_now()
            self.write_evidence()

    def self_review(self) -> Dict[str, Any]:
        self.require(shutil.which("docker") is not None, "docker executable is required")
        self.require(self.compose_file.is_file(), "compose.yaml is missing")
        self.require(PROJECT_PATTERN.fullmatch(self.project) is not None, "unsafe project name")

        relative_artifacts = self.artifact_dir.parent.relative_to(self.root)
        ignored = self.run_command(
            ["git", "check-ignore", "-q", str(relative_artifacts)],
            timeout=10,
            check=False,
            record=False,
        )
        self.require(
            ignored.returncode == 0,
            "artifact directory is not covered by .gitignore",
            str(relative_artifacts),
        )

        compose_version = self.run_command(
            ["docker", "compose", "version"], timeout=20, record=False
        ).stdout.strip()
        self.run_command(["docker", "info"], timeout=30, record=False)
        self.compose("config", "--quiet", timeout=30)
        services = set(
            line.strip()
            for line in self.compose("config", "--services", timeout=30).stdout.splitlines()
            if line.strip()
        )
        required_services = {"postgres", "redis", "migrate", "api", "worker", "frontend"}
        self.require(
            required_services.issubset(services),
            "Compose is missing required services",
            sorted(required_services - services),
        )

        rendered = self.compose("config", "--format", "json", timeout=30, record=False).stdout
        try:
            config = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("docker compose config did not return valid JSON") from exc
        configured_services = config.get("services", {})
        for service_name in ("postgres", "redis"):
            self.require(
                not configured_services.get(service_name, {}).get("ports"),
                "{} must not publish a host port".format(service_name),
            )
        for service_name in ("api", "frontend"):
            ports = configured_services.get(service_name, {}).get("ports", [])
            self.require(ports, "{} must publish its loopback port".format(service_name))
            self.require(
                all(port.get("host_ip") == "127.0.0.1" for port in ports),
                "{} must bind only to IPv4 loopback".format(service_name),
                ports,
            )
            expected_port = self.api_port if service_name == "api" else self.frontend_port
            self.require(
                any(int(port.get("published", 0)) == expected_port for port in ports),
                "{} did not render the generated random port".format(service_name),
                ports,
            )

        for service_name in ("api", "worker", "migrate"):
            environment = configured_services.get(service_name, {}).get("environment", {})
            database_url = str(environment.get("DATABASE_URL", ""))
            redis_url = str(environment.get("REDIS_URL", ""))
            self.require(
                "@postgres:5432/llmbenchlab" in database_url,
                "{} is not pinned to the isolated Compose PostgreSQL".format(service_name),
            )
            self.require(
                redis_url == "redis://redis:6379/0",
                "{} is not pinned to the isolated Compose Redis".format(service_name),
            )

        preexisting = self.compose("ps", "-a", "-q", timeout=20).stdout.strip()
        self.require(not preexisting, "generated project unexpectedly already has containers")

        timestamp_variants = {
            "2026-08-25T06:51:51Z": 0,
            "2026-08-25T06:51:51.8+00:00": 800000,
            "2026-08-25T06:51:51.87456+00:00": 874560,
            "2026-08-25T06:51:51.874560+00:00": 874560,
        }
        for value, expected_microsecond in timestamp_variants.items():
            parsed = parse_datetime(value)
            self.require(
                parsed.microsecond == expected_microsecond,
                "database timestamp parser lost fractional precision",
                {"value": value, "parsed": parsed.isoformat()},
            )

        review = {
            "status": "passed",
            "at": utc_now(),
            "python_version": sys.version.split()[0],
            "compose_version": compose_version,
            "project_name_guard": PROJECT_PATTERN.pattern,
            "required_services": sorted(required_services),
            "five_long_running_services": [
                "postgres",
                "redis",
                "api",
                "worker",
                "frontend",
            ],
            "one_shot_service": "migrate",
            "internal_postgres_and_redis": True,
            "loopback_only_published_ports": True,
            "isolated_project_volumes": True,
            "artifacts_gitignored": str(relative_artifacts),
            "real_provider_credentials_removed": ["OPENAI_API_KEY", "LLMBENCHLAB_DEMO_API_KEY"],
            "database_timestamp_variants_checked": len(timestamp_variants),
        }
        git_commit = self.run_command(
            ["git", "rev-parse", "HEAD"], timeout=10, record=False
        ).stdout.strip()
        git_status = self.run_command(
            ["git", "status", "--short"], timeout=10, record=False
        ).stdout.splitlines()
        self.evidence["repository"] = {
            "commit": git_commit,
            "dirty": bool(git_status),
            "status_paths": [line[:2] + " " + line[3:] for line in git_status],
            "acceptance_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "compose_sha256": hashlib.sha256(self.compose_file.read_bytes()).hexdigest(),
        }
        self.evidence["self_review"] = review
        return review

    def http_json(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        accepted: Optional[Set[int]] = None,
        timeout: float = 5,
        frontend: bool = False,
    ) -> Dict[str, Any]:
        base = self.frontend_base if frontend else self.api_base
        data = None
        client_request_id = "p2-" + uuid.uuid4().hex
        headers = {"Accept": "application/json", "X-Request-ID": client_request_id}
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=timeout) as response:
                status_code = int(response.status)
                raw = response.read().decode("utf-8", errors="replace")
                response_headers = response.headers
        except urllib.error.HTTPError as exc:
            try:
                status_code = int(exc.code)
                raw = exc.read().decode("utf-8", errors="replace")
                response_headers = exc.headers
            finally:
                exc.close()
        elapsed = round(time.monotonic() - started, 3)
        try:
            payload: Any = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        if accepted is not None and status_code not in accepted:
            raise AcceptanceFailure(
                "{} {} returned {}, expected {}: {}".format(
                    method,
                    path,
                    status_code,
                    sorted(accepted),
                    sanitize(payload),
                )
            )
        response_request_id = response_headers.get("X-Request-ID")
        if not frontend:
            if response_request_id == client_request_id:
                raise AcceptanceFailure("API reflected a client-controlled X-Request-ID")
            try:
                server_request_id = uuid.UUID(str(response_request_id))
            except (AttributeError, TypeError, ValueError) as exc:
                raise AcceptanceFailure(
                    "API did not return a server-generated UUID request ID"
                ) from exc
            if server_request_id.version != 4 or str(server_request_id) != response_request_id:
                raise AcceptanceFailure("API request ID was not a canonical UUIDv4")
        return {
            "status_code": status_code,
            "elapsed_seconds": elapsed,
            "request_id": response_request_id,
            "payload": payload,
        }

    def wait_for(
        self,
        description: str,
        operation: Callable[[], Any],
        predicate: Callable[[Any], bool],
        timeout: float = 60,
        interval: float = 0.15,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last_value: Any = None
        last_error: Optional[BaseException] = None
        while time.monotonic() < deadline:
            try:
                last_value = operation()
                last_error = None
                if predicate(last_value):
                    return last_value
            except (AcceptanceFailure, OSError, urllib.error.URLError) as exc:
                last_error = exc
            time.sleep(interval)
        detail = {
            "last_value": sanitize(last_value),
            "last_error": None if last_error is None else redact_text(str(last_error)),
        }
        raise AcceptanceFailure("timed out waiting for {}: {}".format(description, detail))

    def get_run(self, run_id: str) -> Dict[str, Any]:
        result = self.http_json("GET", "/runs/{}".format(run_id), accepted={200})
        return result["payload"]

    def wait_run(
        self,
        run_id: str,
        description: str,
        predicate: Callable[[Dict[str, Any]], bool],
        timeout: float = 90,
    ) -> Dict[str, Any]:
        return self.wait_for(
            description,
            lambda: self.get_run(run_id),
            predicate,
            timeout=timeout,
        )

    def responses(self, run_id: str) -> Dict[str, Any]:
        result = self.http_json(
            "GET",
            "/runs/{}/responses?offset=0&limit=100".format(run_id),
            accepted={200},
        )
        return result["payload"]

    def create_run(self) -> Dict[str, Any]:
        self.require(self.model_id is not None, "Mock model was not initialized")
        self.require(self.benchmark_id is not None, "Demo benchmark was not initialized")
        result = self.http_json(
            "POST",
            "/runs",
            body={
                "model_id": self.model_id,
                "benchmark_id": self.benchmark_id,
                "temperature": 0,
                "top_p": 1,
                "max_tokens": 64,
                "seed": 42,
                "concurrency": 1,
            },
            accepted={202},
            timeout=8,
        )
        run = result["payload"]
        run["_request_elapsed_seconds"] = result["elapsed_seconds"]
        return run

    def canonical_snapshot(self, run_id: str) -> Dict[str, Any]:
        run = self.get_run(run_id)
        response_page = self.responses(run_id)
        items = response_page["items"]
        self.require(
            int(response_page["total"]) == len(items),
            "response page did not contain the complete evidence set",
        )
        selected_run = {field: run.get(field) for field in RUN_SNAPSHOT_FIELDS}
        selected_responses = [
            {field: response.get(field) for field in RESPONSE_SNAPSHOT_FIELDS}
            for response in sorted(items, key=lambda item: (item["question_id"], item["id"]))
        ]
        snapshot = {"run": selected_run, "responses": selected_responses}
        snapshot["sha256"] = canonical_hash(snapshot)
        return snapshot

    def assert_complete_protocol(
        self,
        run_id: str,
        expected_attempts: Optional[int],
    ) -> Dict[str, Any]:
        snapshot = self.canonical_snapshot(run_id)
        run = snapshot["run"]
        responses = snapshot["responses"]
        self.require(run["status"] == "completed", "Run did not complete", run)
        self.require(
            run["protocol_version"] == PROTOCOL_VERSION,
            "protocol version changed",
            run["protocol_version"],
        )
        self.require(
            run["total_questions"] == run["completed_questions"] == 15,
            "Run did not preserve all 15 question facts",
            run,
        )
        self.require(run["correct_questions"] == 15, "correct count changed", run)
        self.require(run["error_questions"] == 0, "unexpected question errors", run)
        for field in ("score", "completion_rate", "answered_accuracy"):
            self.require(
                math.isclose(float(run[field]), 100.0, rel_tol=0, abs_tol=1e-9),
                "{} changed from protocol-v1 100".format(field),
                run[field],
            )
        self.require(run["input_tokens"] == 120, "input token aggregate changed", run)
        self.require(run["output_tokens"] == 30, "output token aggregate changed", run)
        self.require(
            math.isclose(float(run["estimated_cost"]), 0.0, rel_tol=0, abs_tol=1e-12),
            "Mock cost must remain zero",
            run["estimated_cost"],
        )
        if expected_attempts is not None:
            self.require(
                run["attempt_count"] == expected_attempts,
                "attempt count was not the expected recovery count",
                run,
            )
        self.require(run["lease_owner"] is None, "terminal Run retained a lease owner", run)
        self.require(run["lease_expires_at"] is None, "terminal Run retained lease expiry", run)
        self.require(run["heartbeat_at"] is None, "terminal Run retained heartbeat", run)
        self.require(len(responses) == 15, "Response evidence count changed", len(responses))
        self.require(
            len({response["question_id"] for response in responses}) == 15,
            "duplicate question evidence was observed",
        )
        for response in responses:
            self.require(response["error_type"] is None, "unexpected response error", response)
            self.require(
                math.isclose(float(response["score"]), 1.0, rel_tol=0, abs_tol=1e-9),
                "per-question score changed",
                response,
            )
            self.require(response["input_tokens"] == 8, "input token evidence changed", response)
            self.require(response["output_tokens"] == 2, "output token evidence changed", response)
            self.require(
                math.isclose(float(response["estimated_cost"]), 0.0, rel_tol=0, abs_tol=1e-12),
                "per-question Mock cost changed",
                response,
            )

        raw_run = self.get_run(run_id)
        model_snapshot = raw_run.get("model_parameters_snapshot", {}).get("model", {})
        self.require(
            model_snapshot.get("adapter_type") == "mock",
            "acceptance Run did not use the offline Mock adapter",
            model_snapshot,
        )
        self.require(
            model_snapshot.get("api_key_env") is None,
            "offline Mock snapshot unexpectedly references a credential",
            model_snapshot,
        )
        return snapshot

    def psql(self, sql: str, check: bool = True) -> subprocess.CompletedProcess:
        return self.psql_database(sql, database="llmbenchlab", check=check)

    def run_database_seam_helper(
        self,
        mode: str,
        run_id: str,
        *,
        baseline_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create one coherent boundary and expire its lease inside PostgreSQL."""

        allowed_modes = {"reserved", "send_started", "response_committed"}
        self.require(mode in allowed_modes, "unsupported database seam mode", mode)
        self.require(SAFE_ID_PATTERN.fullmatch(run_id) is not None, "unsafe Run ID")
        if baseline_run_id is not None:
            self.require(
                SAFE_ID_PATTERN.fullmatch(baseline_run_id) is not None,
                "unsafe baseline Run ID",
            )
        self.require(
            mode != "response_committed" or baseline_run_id is not None,
            "response commit seam requires a baseline Run",
        )
        completed = self.compose(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            DB_SEAM_HELPER_SOURCE,
            mode,
            run_id,
            baseline_run_id or "-",
            timeout=60,
            check=False,
            # The full inline source is intentionally omitted from command evidence;
            # its SHA-256 is retained below and the script itself is repository tracked.
            record=False,
        )
        self.require(
            completed.returncode == 0,
            "database seam helper failed",
            {
                "mode": mode,
                "returncode": completed.returncode,
                "stderr_tail": redact_text(completed.stderr[-4000:]),
            },
        )
        raw = completed.stdout.strip().splitlines()
        self.require(bool(raw), "database seam helper returned no evidence")
        try:
            result = json.loads(raw[-1])
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("database seam helper evidence was not valid JSON") from exc
        self.require(isinstance(result, dict), "database seam evidence was not a mapping")
        expected = {
            "fault_method": "deterministic_database_seam_injection",
            "sigkill_used": False,
            "mode": mode,
            "run_id": run_id,
        }
        self.require(
            all(result.get(key) == value for key, value in expected.items()),
            "database seam helper returned mismatched evidence",
            result,
        )
        result["helper_source_sha256"] = hashlib.sha256(
            DB_SEAM_HELPER_SOURCE.encode("utf-8")
        ).hexdigest()
        return result

    def db_crash_seam_snapshot(self, run_id: str) -> Dict[str, Any]:
        """Return an allowlisted ledger/Response/audit projection for one Run."""

        self.require(SAFE_ID_PATTERN.fullmatch(run_id) is not None, "unsafe Run ID")
        sql = """
SELECT json_build_object(
  'run', (
    SELECT json_build_object(
      'id', r.id,
      'status', r.status,
      'attempt_count', r.attempt_count,
      'failed_attempt_count', r.failed_attempt_count,
      'lease_token', r.lease_token,
      'completed_questions', r.completed_questions
    )
    FROM evaluation_runs r WHERE r.id = '{run_id}'
  ),
  'question_executions', COALESCE((
    SELECT json_agg(json_build_object(
      'id', q.id,
      'question_id', q.question_id,
      'execution_generation', q.execution_generation,
      'next_provider_attempt', q.next_provider_attempt
    ) ORDER BY q.question_id, q.id)
    FROM question_executions q WHERE q.run_id = '{run_id}'
  ), '[]'::json),
  'reservations', COALESCE((
    SELECT json_agg(json_build_object(
      'id', p.id,
      'question_id', p.question_id,
      'execution_generation', p.execution_generation,
      'provider_attempt', p.provider_attempt,
      'state', p.state,
      'reserved_input_tokens', p.reserved_input_tokens,
      'reserved_output_tokens', p.reserved_output_tokens,
      'reserved_cost_usd', p.reserved_cost_usd,
      'actual_input_tokens', p.actual_input_tokens,
      'actual_output_tokens', p.actual_output_tokens,
      'actual_cost_usd', p.actual_cost_usd,
      'send_started', p.send_started_at IS NOT NULL,
      'settled', p.settled_at IS NOT NULL,
      'reserved_event_count', (
        SELECT count(*) FROM audit_events a
        WHERE a.reservation_id = p.id AND a.event_type = 'provider_attempt_reserved'
      ),
      'send_started_event_count', (
        SELECT count(*) FROM audit_events a
        WHERE a.reservation_id = p.id AND a.event_type = 'provider_attempt_send_started'
      ),
      'settled_event_count', (
        SELECT count(*) FROM audit_events a
        WHERE a.reservation_id = p.id AND a.event_type = 'provider_attempt_settled'
      ),
      'settlement_dispositions', COALESCE((
        SELECT json_agg(a.payload->>'disposition' ORDER BY a.occurred_at, a.id)
        FROM audit_events a
        WHERE a.reservation_id = p.id AND a.event_type = 'provider_attempt_settled'
      ), '[]'::json)
    ) ORDER BY p.question_id, p.execution_generation, p.provider_attempt, p.id)
    FROM provider_call_reservations p WHERE p.run_id = '{run_id}'
  ), '[]'::json),
  'response_count', (
    SELECT count(*) FROM evaluation_responses e WHERE e.run_id = '{run_id}'
  ),
  'distinct_response_questions', (
    SELECT count(DISTINCT e.question_id)
    FROM evaluation_responses e WHERE e.run_id = '{run_id}'
  ),
  'response_ids', COALESCE((
    SELECT json_agg(e.id ORDER BY e.question_id, e.id)
    FROM evaluation_responses e WHERE e.run_id = '{run_id}'
  ), '[]'::json),
  'responses', COALESCE((
    SELECT json_agg(json_build_object(
      'id', e.id,
      'question_id', e.question_id,
      'persisted_event_count', (
        SELECT count(*) FROM audit_events a
        WHERE a.event_key = 'response:' || e.id || ':persisted'
          AND a.event_type = 'question_evidence_persisted'
      )
    ) ORDER BY e.question_id, e.id)
    FROM evaluation_responses e WHERE e.run_id = '{run_id}'
  ), '[]'::json),
  'audit_event_type_counts', COALESCE((
    SELECT json_object_agg(grouped.event_type, grouped.event_count)
    FROM (
      SELECT a.event_type, count(*) AS event_count
      FROM audit_events a WHERE a.run_id = '{run_id}'
      GROUP BY a.event_type ORDER BY a.event_type
    ) grouped
  ), '{{}}'::json)
)::text;
""".format(run_id=run_id)
        raw = self.psql(sql).stdout.strip()
        self.require(bool(raw), "PostgreSQL returned no database seam snapshot")
        try:
            snapshot = json.loads(raw.splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("database seam snapshot was not valid JSON") from exc
        snapshot["sha256"] = canonical_hash(snapshot)
        return snapshot

    def psql_database(
        self,
        sql: str,
        *,
        database: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        self.require(
            SAFE_ID_PATTERN.fullmatch(database) is not None,
            "unsafe PostgreSQL database name",
        )
        return self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "llmbenchlab",
            "-d",
            database,
            "-c",
            sql,
            timeout=30,
            check=check,
        )

    def clone_application_database(self, target_database: str) -> Dict[str, Any]:
        """Clone the application database without racing its healthcheck connection."""

        self.require(
            SAFE_ID_PATTERN.fullmatch(target_database) is not None,
            "unsafe PostgreSQL clone database name",
        )
        self.require(
            target_database not in {"llmbenchlab", "postgres"},
            "refused to replace a protected PostgreSQL database",
        )
        restoration: Optional[subprocess.CompletedProcess] = None
        try:
            self.psql_database(
                'ALTER DATABASE "llmbenchlab" WITH ALLOW_CONNECTIONS false;',
                database="postgres",
            )
            terminated_raw = self.psql_database(
                "SELECT json_build_object("
                "'attempted', count(*), "
                "'all_terminated', COALESCE(bool_and(terminated), true))::text "
                "FROM (SELECT pg_terminate_backend(pid) AS terminated "
                "FROM pg_stat_activity WHERE datname = 'llmbenchlab' "
                "AND pid <> pg_backend_pid()) active_connections;",
                database="postgres",
            ).stdout.strip()
            try:
                terminated = json.loads(terminated_raw)
            except json.JSONDecodeError as exc:
                raise AcceptanceFailure(
                    "PostgreSQL returned invalid source connection termination evidence"
                ) from exc
            self.require(
                terminated.get("all_terminated") is True,
                "could not terminate every application database connection before clone",
                terminated,
            )
            created = self.psql_database(
                f'CREATE DATABASE "{target_database}" TEMPLATE "llmbenchlab";',
                database="postgres",
            )
        finally:
            restoration = self.psql_database(
                'ALTER DATABASE "llmbenchlab" WITH ALLOW_CONNECTIONS true;',
                database="postgres",
                check=False,
            )
            self.require(
                restoration.returncode == 0,
                "failed to restore application database connections after clone",
                redact_text(restoration.stderr or restoration.stdout),
            )

        return {
            "source_connections_disabled": True,
            "terminated_connection_count": int(terminated["attempted"]),
            "source_connections_restored": restoration is not None,
            "create_returncode": created.returncode,
        }

    def db_run_snapshot(self, run_id: str) -> Dict[str, Any]:
        self.require(SAFE_ID_PATTERN.fullmatch(run_id) is not None, "unsafe Run ID")
        sql = """
SELECT json_build_object(
  'database_now', CURRENT_TIMESTAMP,
  'id', r.id,
  'status', r.status,
  'attempt_count', r.attempt_count,
  'lease_token', r.lease_token,
  'lease_owner', r.lease_owner,
  'lease_expires_at', r.lease_expires_at,
  'heartbeat_at', r.heartbeat_at,
  'last_error', r.last_error,
  'response_count', (SELECT count(*) FROM evaluation_responses e WHERE e.run_id = r.id),
  'distinct_question_count', (
    SELECT count(DISTINCT e.question_id) FROM evaluation_responses e WHERE e.run_id = r.id
  ),
  'response_ids', COALESCE((
    SELECT json_agg(e.id ORDER BY e.question_id, e.id)
    FROM evaluation_responses e WHERE e.run_id = r.id
  ), '[]'::json),
  'active_provider_attempts', (
    SELECT count(*) FROM provider_call_reservations p
    WHERE p.run_id = r.id AND p.state IN ('reserved', 'send_started')
  ),
  'send_started_provider_attempts', (
    SELECT count(*) FROM provider_call_reservations p
    WHERE p.run_id = r.id AND p.state = 'send_started'
  )
)::text
FROM evaluation_runs r
WHERE r.id = '{}';
""".format(run_id)
        completed = self.psql(sql)
        raw = completed.stdout.strip()
        self.require(bool(raw), "PostgreSQL did not return the requested Run")
        try:
            return json.loads(raw.splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("PostgreSQL Run snapshot was not valid JSON") from exc

    def db_core_protocol_snapshot(self, run_id: str) -> Dict[str, Any]:
        """Return only columns that exist unchanged at both 0001 and head."""

        self.require(SAFE_ID_PATTERN.fullmatch(run_id) is not None, "unsafe Run ID")
        sql = """
SELECT json_build_object(
  'run', json_build_object(
    'id', r.id,
    'model_id', r.model_id,
    'benchmark_id', r.benchmark_id,
    'status', r.status,
    'protocol_version', r.protocol_version,
    'model_parameters_snapshot', r.model_parameters_snapshot,
    'benchmark_hash_snapshot', r.benchmark_hash_snapshot,
    'prompt_template_snapshot', r.prompt_template_snapshot,
    'code_commit_sha', r.code_commit_sha,
    'total_questions', r.total_questions,
    'completed_questions', r.completed_questions,
    'correct_questions', r.correct_questions,
    'error_questions', r.error_questions,
    'score', r.score,
    'completion_rate', r.completion_rate,
    'answered_accuracy', r.answered_accuracy,
    'average_latency_ms', r.average_latency_ms,
    'input_tokens', r.input_tokens,
    'output_tokens', r.output_tokens,
    'estimated_cost', r.estimated_cost,
    'cancellation_requested', r.cancellation_requested,
    'started_at', r.started_at,
    'finished_at', r.finished_at,
    'created_at', r.created_at,
    'error_message', r.error_message
  ),
  'responses', COALESCE((
    SELECT json_agg(json_build_object(
      'id', e.id,
      'run_id', e.run_id,
      'question_id', e.question_id,
      'raw_response', e.raw_response,
      'parsed_answer', e.parsed_answer,
      'reference_answer_snapshot', e.reference_answer_snapshot,
      'score', e.score,
      'evaluator_name', e.evaluator_name,
      'latency_ms', e.latency_ms,
      'input_tokens', e.input_tokens,
      'output_tokens', e.output_tokens,
      'estimated_cost', e.estimated_cost,
      'error_type', e.error_type,
      'error_message', e.error_message,
      'created_at', e.created_at
    ) ORDER BY e.question_id, e.id)
    FROM evaluation_responses e
    WHERE e.run_id = r.id
  ), '[]'::json)
)::text
FROM evaluation_runs r
WHERE r.id = '{}';
""".format(run_id)
        completed = self.psql(sql)
        raw = completed.stdout.strip()
        self.require(bool(raw), "PostgreSQL did not return the core protocol snapshot")
        try:
            snapshot = json.loads(raw.splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("PostgreSQL core snapshot was not valid JSON") from exc
        self.require(
            snapshot["run"]["protocol_version"] == PROTOCOL_VERSION,
            "core snapshot protocol version changed",
            snapshot["run"],
        )
        self.require(len(snapshot["responses"]) == 15, "core snapshot lost Response evidence")
        snapshot["sha256"] = canonical_hash(snapshot)
        return snapshot

    def redis_cli(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
        return self.compose(
            "exec",
            "-T",
            "redis",
            "redis-cli",
            "--raw",
            *arguments,
            timeout=20,
            check=check,
        )

    def redis_json(self, *arguments: str) -> Any:
        completed = self.compose(
            "exec",
            "-T",
            "redis",
            "redis-cli",
            "-3",
            "--json",
            *arguments,
            timeout=20,
        )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure(
                "Redis did not return valid JSON: {}".format(
                    redact_text(completed.stdout or completed.stderr)
                )
            ) from exc

    @staticmethod
    def redis_mapping(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return {str(key): item for key, item in value.items()}
        if isinstance(value, list) and len(value) % 2 == 0:
            return {str(value[index]): value[index + 1] for index in range(0, len(value), 2)}
        raise AcceptanceFailure("Redis metadata response was not a mapping: {}".format(value))

    def stream_last_id(self) -> str:
        info = self.redis_mapping(self.redis_json("XINFO", "STREAM", TASK_STREAM))
        message_id = str(info.get("last-generated-id", ""))
        self.require(bool(message_id), "Redis Stream has no last-generated-id", info)
        return message_id

    def group_info(self) -> Dict[str, Any]:
        raw_groups = self.redis_json("XINFO", "GROUPS", TASK_STREAM)
        self.require(isinstance(raw_groups, list), "Redis XINFO GROUPS was not a list")
        groups = [self.redis_mapping(item) for item in raw_groups]
        matches = [group for group in groups if group.get("name") == TASK_GROUP]
        self.require(len(matches) == 1, "Redis consumer group was not uniquely present", groups)
        group = matches[0]
        return {
            "name": str(group["name"]),
            "last_delivered_id": str(group["last-delivered-id"]),
            "pending": int(group["pending"]),
            "consumers": int(group["consumers"]),
            "lag": None if group.get("lag") is None else int(group["lag"]),
        }

    @staticmethod
    def stream_id_at_least(observed: str, expected: str) -> bool:
        try:
            observed_parts = tuple(int(part) for part in observed.split("-", 1))
            expected_parts = tuple(int(part) for part in expected.split("-", 1))
        except ValueError as exc:
            raise AcceptanceFailure("invalid Redis Stream ID") from exc
        return observed_parts >= expected_parts

    def wait_message_delivered_and_acked(
        self, message_id: str, timeout: float = 60
    ) -> Dict[str, Any]:
        self.require("-" in message_id, "invalid target Redis message ID", message_id)
        return self.wait_for(
            "Redis message {} delivery and ACK".format(message_id),
            self.group_info,
            lambda group: (
                self.stream_id_at_least(group["last_delivered_id"], message_id)
                and group["pending"] == 0
            ),
            timeout=timeout,
            interval=0.2,
        )

    def wait_queue_drained(self, timeout: float = 60) -> Dict[str, Any]:
        return self.wait_message_delivered_and_acked(self.stream_last_id(), timeout=timeout)

    def service_container_ids(self, service: str, include_stopped: bool = False) -> List[str]:
        arguments = ["ps"]
        if include_stopped:
            arguments.append("-a")
        arguments.extend(["-q", service])
        completed = self.compose(*arguments, timeout=20)
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()]

    def container_meta(self, container_id: str) -> Dict[str, Any]:
        # Full inspect JSON is parsed in memory with command recording disabled,
        # because Config.Env contains the local-only database password.  Only the
        # allowlisted, non-secret metadata below is retained in evidence.
        completed = self.run_command(
            ["docker", "inspect", container_id],
            timeout=20,
            record=False,
        )
        try:
            inspected = json.loads(completed.stdout)[0]
        except (IndexError, KeyError, json.JSONDecodeError) as exc:
            raise AcceptanceFailure("unexpected docker inspect output") from exc
        state = inspected["State"]
        config = inspected["Config"]
        labels = config.get("Labels") or {}
        metadata = {
            "id": inspected["Id"],
            "name": inspected["Name"].lstrip("/"),
            "hostname": config["Hostname"],
            "status": state["Status"],
            "pid": int(state["Pid"]),
            "restart_count": int(inspected["RestartCount"]),
            "exit_code": int(state["ExitCode"]),
            "started_at": state["StartedAt"],
            "health": (state.get("Health") or {}).get("Status", "none"),
            "image_id": inspected["Image"],
            "project": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"),
        }
        self.require(metadata["project"] == self.project, "container escaped project isolation")
        return metadata

    def service_metas(self, service: str, include_stopped: bool = False) -> List[Dict[str, Any]]:
        metas = [
            self.container_meta(container_id)
            for container_id in self.service_container_ids(service, include_stopped)
        ]
        return sorted(metas, key=lambda item: item["id"])

    def single_service_meta(self, service: str) -> Dict[str, Any]:
        metas = self.service_metas(service)
        self.require(len(metas) == 1, "{} must have exactly one running container".format(service))
        return metas[0]

    def wait_service_healthy(self, service: str, count: int = 1, timeout: float = 90) -> Any:
        return self.wait_for(
            "{} healthy container count {}".format(service, count),
            lambda: self.service_metas(service),
            lambda metas: (
                len(metas) == count
                and all(
                    meta["status"] == "running" and meta["health"] == "healthy" for meta in metas
                )
            ),
            timeout=timeout,
            interval=0.5,
        )

    def start_validated_containers(
        self, containers: Sequence[Dict[str, Any]], expected_service: str
    ) -> None:
        self.require(bool(containers), "no containers were provided for explicit start")
        for metadata in containers:
            self.require(
                metadata["project"] == self.project
                and metadata["service"] == expected_service
                and metadata["status"] == "exited",
                "refused to start an unvalidated container",
                metadata,
            )
        self.run_command(
            ["docker", "start", *[metadata["id"] for metadata in containers]],
            timeout=60,
        )

    def wait_api_ready(self, expected_status: int = 200, timeout: float = 90) -> Dict[str, Any]:
        return self.wait_for(
            "API readiness status {}".format(expected_status),
            lambda: self.http_json("GET", "/ready", accepted={200, 503}, timeout=4),
            lambda result: result["status_code"] == expected_status,
            timeout=timeout,
            interval=0.25,
        )

    def wait_worker_progress_healthy(self, timeout: float = 90) -> Dict[str, Any]:
        """Wait for both configured Worker main loops to report current progress."""

        result = self.wait_for(
            "two registered and live Worker main loops without stalls or shortfall",
            lambda: self.http_json("GET", "/tasks/metrics", accepted={200}, timeout=4),
            lambda response: all(
                response["payload"].get(field) == expected
                for field, expected in {
                    "worker_expected_processes": 2,
                    "worker_registered_processes": 2,
                    "worker_live_processes": 2,
                    "worker_stalled_processes": 0,
                    "worker_shortfall_processes": 0,
                }.items()
            ),
            timeout=timeout,
            interval=0.25,
        )
        return result["payload"]

    def setup_stack(self) -> None:
        self.stack_touched = True
        self.compose(
            "up",
            "--build",
            "-d",
            "--wait",
            "--scale",
            "worker=2",
            timeout=600,
            max_recorded_chars=24000,
        )

    def initialize_demo(self) -> Dict[str, Any]:
        benchmark_result = self.http_json(
            "POST", "/benchmarks/reload-demo", accepted={200}, timeout=15
        )
        benchmark = benchmark_result["payload"]
        self.require(benchmark["question_count"] == 15, "Demo question count changed", benchmark)
        model_result = self.http_json(
            "POST",
            "/models",
            body={
                "name": "Phase 2 Acceptance Mock " + self.project[-12:],
                "provider_type": "mock",
                "enabled": True,
                "input_price_per_million": 0,
                "output_price_per_million": 0,
                "default_parameters": {
                    "temperature": 0,
                    "top_p": 1,
                    "max_tokens": 64,
                    "seed": 42,
                },
            },
            accepted={201},
        )
        model = model_result["payload"]
        self.require(model["provider_type"] == "mock", "real provider registration is forbidden")
        self.require(model.get("api_key_env") is None, "Mock model must not reference a key")
        self.benchmark_id = benchmark["id"]
        self.model_id = model["id"]
        return {
            "benchmark": {
                "id": benchmark["id"],
                "slug": benchmark["slug"],
                "version": benchmark["version"],
                "schema_version": benchmark["schema_version"],
                "dataset_hash": benchmark["dataset_hash"],
                "question_count": benchmark["question_count"],
            },
            "model": {
                "id": model["id"],
                "provider_type": model["provider_type"],
                "enabled": model["enabled"],
                "api_key_env": model.get("api_key_env"),
            },
        }

    def topology_scenario(self) -> None:
        with self.scenario("compose_topology_and_health") as entry:
            required_health = {}
            for service in ("postgres", "redis", "api"):
                required_health[service] = self.wait_service_healthy(service)
            required_health["worker"] = self.wait_service_healthy("worker", count=2)
            frontend_meta = self.single_service_meta("frontend")
            self.require(frontend_meta["status"] == "running", "frontend is not running")
            migrate = self.service_metas("migrate", include_stopped=True)
            self.require(len(migrate) == 1, "migrate service did not run exactly once", migrate)
            self.require(
                migrate[0]["status"] == "exited" and migrate[0]["exit_code"] == 0,
                "migrate service did not complete successfully",
                migrate,
            )

            live = self.http_json("GET", "/live", accepted={200})
            health = self.http_json("GET", "/health", accepted={200})
            ready = self.wait_api_ready(200)
            worker_progress = self.wait_worker_progress_healthy()
            info = self.http_json("GET", "/info", accepted={200})
            frontend = self.http_json("GET", "/healthz", accepted={200}, frontend=True)
            self.require(live["payload"]["status"] == "live", "liveness payload changed", live)
            self.require(health["payload"]["database"] == "ok", "database health failed")
            self.require(ready["payload"]["status"] == "ready", "API was not ready", ready)
            self.require(ready["payload"]["queue"] == "ok", "Redis readiness failed", ready)
            self.require(
                info["payload"]["protocol_version"] == PROTOCOL_VERSION,
                "info protocol version changed",
            )
            self.require(str(frontend["payload"]).strip() == "ok", "frontend healthz failed")

            versions_raw = self.psql(
                "SELECT COALESCE(json_agg(version_num ORDER BY version_num), '[]'::json)::text "
                "FROM alembic_version;"
            ).stdout.strip()
            versions = json.loads(versions_raw)
            self.require(len(versions) == 1, "database does not have exactly one Alembic head")
            demo = self.initialize_demo()
            topology = {
                "healthy_services": required_health,
                "frontend": frontend_meta,
                "migrate": migrate[0],
                "alembic_heads": versions,
                "live": live["payload"],
                "health": health["payload"],
                "ready": ready["payload"],
                "worker_progress": worker_progress,
                "frontend_healthz": frontend["payload"],
                "fixtures": demo,
            }
            self.evidence["topology"] = topology
            entry["evidence"] = topology

    def baseline_scenario(self) -> None:
        with self.scenario("protocol_v1_baseline") as entry:
            created = self.create_run()
            message_id = self.stream_last_id()
            final = self.wait_run(
                created["id"],
                "baseline Run completion",
                lambda run: run["status"] == "completed",
            )
            snapshot = self.assert_complete_protocol(final["id"], expected_attempts=1)
            queue = self.wait_message_delivered_and_acked(message_id)
            self.baseline_run_id = final["id"]
            entry["evidence"] = {
                "run_id": final["id"],
                "message_id": message_id,
                "protocol_snapshot": snapshot,
                "queue_after_ack": queue,
            }

    def api_restart_scenario(self) -> None:
        with self.scenario("api_restart_during_execution") as entry:
            created = self.create_run()
            message_id = self.stream_last_id()
            partial = self.wait_run(
                created["id"],
                "Run to be actively executing before API restart",
                lambda run: (
                    run["status"] == "running"
                    and 0 < run["completed_questions"] < run["total_questions"]
                ),
            )
            before_api = self.single_service_meta("api")
            before_workers = self.service_metas("worker")
            before_responses = self.responses(created["id"])["items"]
            self.compose("restart", "--no-deps", "api", timeout=60)
            ready = self.wait_api_ready(200)
            after_api = self.single_service_meta("api")
            after_workers = self.service_metas("worker")
            self.require(before_api["id"] == after_api["id"], "API restart replaced its container")
            self.require(before_api["pid"] != after_api["pid"], "API process did not restart")
            self.require(
                [(item["id"], item["pid"]) for item in before_workers]
                == [(item["id"], item["pid"]) for item in after_workers],
                "API restart unexpectedly restarted Worker processes",
            )
            final = self.wait_run(
                created["id"],
                "Run completion after API restart",
                lambda run: run["status"] == "completed",
            )
            snapshot = self.assert_complete_protocol(final["id"], expected_attempts=1)
            final_ids = {response["id"] for response in snapshot["responses"]}
            self.require(
                {response["id"] for response in before_responses}.issubset(final_ids),
                "API restart replaced already durable Response evidence",
            )
            self.require(
                final["lease_token"] == partial["lease_token"] == 1,
                "API restart changed the Worker lease token",
            )
            queue = self.wait_message_delivered_and_acked(message_id)
            entry["evidence"] = {
                "run_id": created["id"],
                "message_id": message_id,
                "barrier": {
                    "status": partial["status"],
                    "completed_questions": partial["completed_questions"],
                    "lease_owner": partial["lease_owner"],
                    "lease_token": partial["lease_token"],
                },
                "api_before": before_api,
                "api_after": after_api,
                "worker_processes_unchanged": True,
                "ready_after_restart": ready["payload"],
                "protocol_snapshot_sha256": snapshot["sha256"],
                "durable_response_ids_before_restart": sorted(
                    response["id"] for response in before_responses
                ),
                "queue_after_ack": queue,
            }

    def worker_crash_scenario(self) -> None:
        with self.scenario("lease_owner_sigkill_and_natural_takeover") as entry:
            created = self.create_run()
            message_id = self.stream_last_id()
            partial = self.wait_run(
                created["id"],
                "Run to persist partial evidence before Worker SIGKILL",
                lambda run: (
                    run["status"] == "running"
                    and 0 < run["completed_questions"] < run["total_questions"]
                ),
            )
            owner = partial.get("lease_owner")
            self.require(
                isinstance(owner, str) and bool(owner),
                "running Run had no lease owner",
                partial,
            )
            owner_parts = owner.split(":")
            self.require(len(owner_parts) >= 4, "Worker lease owner format was unexpected", owner)
            owner_hostname = owner_parts[1]
            worker_metas = self.service_metas("worker")
            owners = [meta for meta in worker_metas if meta["hostname"] == owner_hostname]
            self.require(len(owners) == 1, "could not map lease owner to one Worker", worker_metas)
            victim = owners[0]
            peers = [meta for meta in worker_metas if meta["id"] != victim["id"]]
            self.require(len(peers) == 1, "takeover requires exactly one peer Worker", worker_metas)
            peer = peers[0]
            self.require(victim["service"] == "worker", "SIGKILL target is not a Worker")
            before_db = self.db_run_snapshot(created["id"])

            self.run_command(["docker", "kill", "--signal", "KILL", victim["id"]], timeout=30)
            killed = self.container_meta(victim["id"])
            self.require(killed["status"] == "exited", "SIGKILL victim did not exit", killed)
            self.require(killed["exit_code"] != 0, "SIGKILL victim exited successfully", killed)
            post_kill_db = self.db_run_snapshot(created["id"])
            self.require(
                post_kill_db["status"] == "running"
                and post_kill_db["lease_owner"] == owner
                and post_kill_db["lease_token"] == 1,
                "SIGKILL did not leave the durable lease for natural expiry",
                post_kill_db,
            )
            before_responses = set(post_kill_db["response_ids"])
            old_expiry = parse_datetime(post_kill_db["lease_expires_at"])

            takeover = self.wait_run(
                created["id"],
                "natural lease expiry and peer Worker takeover",
                lambda run: (
                    run["status"] == "running"
                    and run["attempt_count"] == 2
                    and run["lease_token"] == 2
                    and run["lease_owner"] not in (None, owner)
                ),
                timeout=90,
            )
            takeover_db = self.db_run_snapshot(created["id"])
            database_now = parse_datetime(takeover_db["database_now"])
            self.require(
                database_now >= old_expiry,
                "peer claimed before the old database lease naturally expired",
                {"database_now": database_now.isoformat(), "old_expiry": old_expiry.isoformat()},
            )
            self.require(
                takeover["lease_owner"].split(":")[1] == peer["hostname"],
                "takeover owner was not the surviving peer Worker",
                {"takeover": takeover["lease_owner"], "peer": peer},
            )

            final = self.wait_run(
                created["id"],
                "Run completion after lease takeover",
                lambda run: run["status"] == "completed",
                timeout=90,
            )
            snapshot = self.assert_complete_protocol(final["id"], expected_attempts=2)
            final_ids = {response["id"] for response in snapshot["responses"]}
            self.require(
                before_responses.issubset(final_ids),
                "lease takeover replaced pre-crash Response evidence",
            )
            self.require(
                final.get("last_error") == "worker_lease_expired",
                "lease expiry audit code was not preserved",
                final,
            )
            queue = self.wait_message_delivered_and_acked(message_id, timeout=60)

            self.start_validated_containers([killed], expected_service="worker")
            restored_workers = self.wait_service_healthy("worker", count=2, timeout=90)
            entry["evidence"] = {
                "run_id": created["id"],
                "message_id": message_id,
                "partial_database_snapshot": before_db,
                "post_kill_database_snapshot": post_kill_db,
                "victim_before": victim,
                "victim_after_sigkill": killed,
                "surviving_peer": peer,
                "takeover": {
                    "api": {field: takeover.get(field) for field in RUN_SNAPSHOT_FIELDS},
                    "database": takeover_db,
                    "old_lease_expiry": old_expiry.isoformat(),
                    "database_time_after_takeover": database_now.isoformat(),
                },
                "protocol_snapshot_sha256": snapshot["sha256"],
                "pre_crash_response_ids_preserved": sorted(before_responses),
                "restored_worker_count": len(restored_workers),
                "queue_after_ack": queue,
            }

    def database_crash_seams_scenario(self) -> None:
        """Exercise exact DB boundaries that process-level timing cannot target."""

        with self.scenario(
            "deterministic_database_seam_provider_and_response_commit_recovery"
        ) as entry:
            self.require(self.baseline_run_id is not None, "baseline Run is unavailable")
            self.compose("stop", "worker", timeout=90)
            stopped_workers = self.service_metas("worker", include_stopped=True)
            self.require(
                len(stopped_workers) == 2
                and all(worker["status"] == "exited" for worker in stopped_workers),
                "Workers did not stop before database seam injection",
                stopped_workers,
            )

            injections: Dict[str, Dict[str, Any]] = {}
            created_runs: Dict[str, Dict[str, Any]] = {}
            for mode in ("reserved", "send_started", "response_committed"):
                created = self.create_run()
                created_runs[mode] = created
                injections[mode] = self.run_database_seam_helper(
                    mode,
                    created["id"],
                    baseline_run_id=(
                        self.baseline_run_id if mode == "response_committed" else None
                    ),
                )

            response_run_id = created_runs["response_committed"]["id"]
            duplicate_message_id = self.redis_cli(
                "XADD",
                TASK_STREAM,
                "*",
                "version",
                TASK_MESSAGE_VERSION,
                "run_id",
                response_run_id,
                "correlation_id",
                response_run_id,
            ).stdout.strip()
            self.require(bool(duplicate_message_id), "Redis rejected the duplicate seam delivery")

            self.start_validated_containers(stopped_workers, expected_service="worker")
            restored_workers = self.wait_service_healthy("worker", count=2, timeout=90)
            protocol_snapshots: Dict[str, Dict[str, Any]] = {}
            for mode, created in created_runs.items():
                final = self.wait_run(
                    created["id"],
                    "{} database seam recovery".format(mode),
                    lambda run: run["status"] == "completed",
                    timeout=120,
                )
                protocol_snapshots[mode] = self.assert_complete_protocol(
                    final["id"], expected_attempts=2
                )
            queue = self.wait_message_delivered_and_acked(duplicate_message_id, timeout=60)

            snapshots = {
                mode: self.db_crash_seam_snapshot(created["id"])
                for mode, created in created_runs.items()
            }

            def injected_reservation(mode: str) -> Dict[str, Any]:
                reservation_id = injections[mode]["reservation_id"]
                matching = [
                    row for row in snapshots[mode]["reservations"] if row["id"] == reservation_id
                ]
                self.require(
                    len(matching) == 1,
                    "injected reservation was not uniquely preserved",
                    {"mode": mode, "reservation_id": reservation_id},
                )
                return matching[0]

            reserved = injected_reservation("reserved")
            self.require(
                reserved["state"] == "released_pre_send"
                and reserved["send_started"] is False
                and reserved["settled"] is True,
                "pre-send reservation did not reconcile to released_pre_send",
                reserved,
            )
            self.require(
                reserved["reserved_event_count"] == 1
                and reserved["send_started_event_count"] == 0
                and reserved["settled_event_count"] == 1
                and reserved["settlement_dispositions"] == ["released_pre_send"],
                "pre-send reservation audit was not exactly-once",
                reserved,
            )
            reused_ordinal = [
                row
                for row in snapshots["reserved"]["reservations"]
                if row["question_id"] == reserved["question_id"]
                and row["id"] != reserved["id"]
                and row["provider_attempt"] == reserved["provider_attempt"]
                and row["execution_generation"] > reserved["execution_generation"]
                and row["state"] == "settled_actual"
            ]
            self.require(
                len(reused_ordinal) == 1,
                "confirmed unsent Provider ordinal was not reused in a new generation",
                snapshots["reserved"]["reservations"],
            )

            send_started = injected_reservation("send_started")
            self.require(
                send_started["state"] == "settled_conservative"
                and send_started["send_started"] is True
                and send_started["settled"] is True,
                "send-started reservation did not reconcile conservatively",
                send_started,
            )
            self.require(
                send_started["reserved_event_count"] == 1
                and send_started["send_started_event_count"] == 1
                and send_started["settled_event_count"] == 1
                and send_started["settlement_dispositions"] == ["settled_conservative"],
                "send-started conservative settlement was not exactly-once",
                send_started,
            )
            self.require(
                conservative_settlement_matches_reserved_bounds(send_started),
                "conservative settlement did not consume the reserved bounds",
                send_started,
            )

            response_injection = injections["response_committed"]
            response_snapshot = snapshots["response_committed"]
            response_reservation = injected_reservation("response_committed")
            seeded_question_reservations = [
                row
                for row in response_snapshot["reservations"]
                if row["question_id"] == response_injection["question_id"]
            ]
            seeded_responses = [
                response
                for response in response_snapshot["responses"]
                if response["id"] == response_injection["response_id"]
            ]
            self.require(
                response_snapshot["response_count"] == 15
                and response_snapshot["distinct_response_questions"] == 15,
                "Response commit seam produced duplicate or missing question evidence",
                response_snapshot,
            )
            self.require(
                len(seeded_question_reservations) == 1
                and seeded_question_reservations[0]["id"] == response_reservation["id"]
                and response_reservation["state"] == "settled_actual",
                "duplicate delivery created a second ledger row for committed evidence",
                seeded_question_reservations,
            )
            self.require(
                len(seeded_responses) == 1 and seeded_responses[0]["persisted_event_count"] == 1,
                "committed Response or its persistence audit was not exactly-once",
                seeded_responses,
            )
            self.require(
                response_reservation["reserved_event_count"] == 1
                and response_reservation["send_started_event_count"] == 1
                and response_reservation["settled_event_count"] == 1,
                "committed Response ledger audit was not exactly-once",
                response_reservation,
            )

            entry["evidence"] = {
                "fault_injection": {
                    "method": "deterministic_database_seam_injection",
                    "sigkill_used": False,
                    "reason": (
                        "exact transaction boundaries are injected without fragile process-kill "
                        "timing"
                    ),
                },
                "injections": injections,
                "database_snapshots": snapshots,
                "protocol_snapshot_sha256": {
                    mode: snapshot["sha256"] for mode, snapshot in protocol_snapshots.items()
                },
                "pre_send_ordinal_reused": True,
                "send_started_conservative_settlement_count": 1,
                "response_commit_exactly_once": True,
                "duplicate_message_id": duplicate_message_id,
                "queue_after_duplicate_ack": queue,
                "restored_worker_count": len(restored_workers),
            }

    def redis_outage_scenario(self) -> None:
        with self.scenario("redis_stop_start_and_database_reconciliation") as entry:
            # Stopping idle Workers first removes the notification-audit race: the
            # failed publish is durably recorded while the Run is still pending.
            self.compose("stop", "worker", timeout=90)
            stopped_workers = self.service_metas("worker", include_stopped=True)
            self.require(
                len(stopped_workers) == 2
                and all(meta["status"] == "exited" for meta in stopped_workers),
                "Workers did not stop before the Redis fault",
                stopped_workers,
            )
            self.compose("stop", "redis", timeout=60)
            redis_meta = self.service_metas("redis", include_stopped=True)
            self.require(
                len(redis_meta) == 1 and redis_meta[0]["status"] == "exited",
                "Redis container did not stop",
                redis_meta,
            )

            degraded = self.wait_api_ready(503, timeout=30)
            live = self.http_json("GET", "/live", accepted={200})
            health = self.http_json("GET", "/health", accepted={200})
            ready_payload = degraded["payload"]
            self.require("queue_unavailable" in ready_payload["errors"], "queue error missing")
            self.require(ready_payload["accepting_runs"] is True, "API stopped accepting Runs")
            self.require(
                ready_payload["database_reconciliation"] == "available",
                "database reconciliation was not advertised",
            )

            created = self.create_run()
            self.require(
                created["_request_elapsed_seconds"] < 4,
                "Redis outage exceeded the bounded POST /runs latency",
                created["_request_elapsed_seconds"],
            )
            self.require(created["status"] == "pending", "outage Run was not pending", created)
            self.require(created["attempt_count"] == 0, "outage Run was claimed too early", created)
            self.require(
                created.get("last_error") == "queue_notification_unavailable",
                "queue failure was not durably recorded",
                created,
            )
            pending_db = self.db_run_snapshot(created["id"])
            self.require(pending_db["response_count"] == 0, "pending outage Run had Responses")

            # Existing Worker containers start while Redis is still stopped.  Their
            # database-first scan must complete the Run without a queue delivery.
            self.start_validated_containers(stopped_workers, expected_service="worker")
            workers_degraded = self.wait_service_healthy("worker", count=2, timeout=90)
            final = self.wait_run(
                created["id"],
                "database-only reconciliation while Redis remains stopped",
                lambda run: run["status"] == "completed",
                timeout=90,
            )
            snapshot = self.assert_complete_protocol(final["id"], expected_attempts=1)
            redis_still_stopped = self.service_metas("redis", include_stopped=True)[0]
            self.require(
                redis_still_stopped["status"] == "exited",
                "Redis recovered before database-only reconciliation completed",
            )

            self.compose("start", "redis", timeout=60)
            redis_healthy = self.wait_service_healthy("redis", timeout=90)
            ready = self.wait_api_ready(200, timeout=45)
            queue_after_start = self.wait_queue_drained(timeout=60)

            stream_length_before = int(self.redis_cli("XLEN", TASK_STREAM).stdout.strip())
            recovery_created = self.create_run()
            recovery_message_id = self.stream_last_id()
            stream_length_after = int(self.redis_cli("XLEN", TASK_STREAM).stdout.strip())
            self.require(
                stream_length_after >= stream_length_before + 1,
                "Redis recovery Run was not published to the Stream",
                {
                    "before": stream_length_before,
                    "after": stream_length_after,
                },
            )
            recovery_final = self.wait_run(
                recovery_created["id"],
                "Run completion after Redis recovery",
                lambda run: run["status"] == "completed",
            )
            recovery_snapshot = self.assert_complete_protocol(
                recovery_final["id"], expected_attempts=1
            )
            recovered_queue = self.wait_message_delivered_and_acked(recovery_message_id)
            entry["evidence"] = {
                "run_id": created["id"],
                "ready_while_redis_stopped": ready_payload,
                "live_while_redis_stopped": live["payload"],
                "health_while_redis_stopped": health["payload"],
                "post_elapsed_seconds": created["_request_elapsed_seconds"],
                "pending_database_snapshot": pending_db,
                "worker_containers_started_degraded": workers_degraded,
                "completed_while_redis_status": redis_still_stopped["status"],
                "outage_protocol_snapshot_sha256": snapshot["sha256"],
                "redis_after_start": redis_healthy,
                "ready_after_start": ready["payload"],
                "queue_after_start": queue_after_start,
                "recovery_run_id": recovery_created["id"],
                "recovery_message_id": recovery_message_id,
                "stream_length_before_recovery_run": stream_length_before,
                "stream_length_after_recovery_run": stream_length_after,
                "recovery_protocol_snapshot_sha256": recovery_snapshot["sha256"],
                "recovery_queue_after_ack": recovered_queue,
            }

    def pending_cancel_scenario(self) -> None:
        with self.scenario("pending_cancel") as entry:
            self.compose("stop", "worker", timeout=90)
            stopped_workers = self.service_metas("worker", include_stopped=True)
            self.require(
                len(stopped_workers) == 2
                and all(worker["status"] == "exited" for worker in stopped_workers),
                "Workers did not stop before pending cancellation",
                stopped_workers,
            )
            created = self.create_run()
            original_message_id = self.stream_last_id()
            self.require(
                created["status"] == "pending" and created["attempt_count"] == 0,
                "Run was not durably pending before cancellation",
                created,
            )
            cancelled = self.http_json(
                "POST", "/runs/{}/cancel".format(created["id"]), accepted={200}
            )["payload"]
            self.require(cancelled["status"] == "cancelled", "pending cancel did not terminate")
            self.require(cancelled["attempt_count"] == 0, "pending cancel consumed an attempt")
            self.require(cancelled["completed_questions"] == 0, "pending cancel wrote Responses")
            self.require(cancelled["lease_owner"] is None, "pending cancel retained a lease")

            self.start_validated_containers(stopped_workers, expected_service="worker")
            self.wait_service_healthy("worker", count=2, timeout=90)
            queue = self.wait_message_delivered_and_acked(original_message_id, timeout=60)
            after_delivery = self.get_run(created["id"])
            self.require(
                after_delivery["status"] == "cancelled"
                and after_delivery["attempt_count"] == 0
                and after_delivery["completed_questions"] == 0,
                "queued notification changed the pending-cancelled Run",
                after_delivery,
            )
            entry["evidence"] = {
                "run_id": created["id"],
                "original_message_id": original_message_id,
                "cancelled": {field: cancelled.get(field) for field in RUN_SNAPSHOT_FIELDS},
                "after_delivery": {
                    field: after_delivery.get(field) for field in RUN_SNAPSHOT_FIELDS
                },
                "queue_after_ack": queue,
            }

    def running_cancel_scenario(self) -> None:
        with self.scenario("running_cancel_and_duplicate_delivery") as entry:
            created = self.create_run()
            partial = self.wait_run(
                created["id"],
                "partial execution before running cancellation",
                lambda run: (
                    run["status"] == "running"
                    and 0 < run["completed_questions"] < run["total_questions"]
                ),
            )
            requested = self.http_json(
                "POST", "/runs/{}/cancel".format(created["id"]), accepted={200}
            )["payload"]
            self.require(requested["cancellation_requested"] is True, "cancel flag was not set")
            final = self.wait_run(
                created["id"],
                "running cancellation terminal state",
                lambda run: run["status"] == "cancelled",
                timeout=45,
            )
            self.require(
                0 < final["completed_questions"] < final["total_questions"],
                "running cancellation did not preserve a partial evidence boundary",
                final,
            )
            count = int(final["completed_questions"])
            self.require(final["attempt_count"] == 1, "running cancel changed attempts", final)
            self.require(final["lease_owner"] is None, "cancelled Run retained its lease", final)
            self.require(final["heartbeat_at"] is None, "cancelled Run retained heartbeat", final)
            self.require(
                final["correct_questions"] == count,
                "partial correct count drifted",
                final,
            )
            self.require(
                final["error_questions"] == 0,
                "partial cancellation created errors",
                final,
            )
            expected_percent = count / 15 * 100
            self.require(
                math.isclose(float(final["score"]), expected_percent, abs_tol=1e-9),
                "partial protocol score drifted",
                final,
            )
            self.require(
                math.isclose(float(final["completion_rate"]), expected_percent, abs_tol=1e-9),
                "partial completion rate drifted",
                final,
            )
            self.require(
                math.isclose(float(final["answered_accuracy"]), 100.0, abs_tol=1e-9),
                "partial answered accuracy drifted",
                final,
            )
            self.require(final["input_tokens"] == count * 8, "partial input tokens drifted")
            self.require(final["output_tokens"] == count * 2, "partial output tokens drifted")
            before_duplicate = self.canonical_snapshot(created["id"])

            duplicate_id = self.redis_cli(
                "XADD",
                TASK_STREAM,
                "*",
                "version",
                TASK_MESSAGE_VERSION,
                "run_id",
                created["id"],
                "correlation_id",
                created["id"],
            ).stdout.strip()
            self.require(bool(duplicate_id), "Redis did not accept duplicate notification")
            queue = self.wait_message_delivered_and_acked(duplicate_id, timeout=60)
            after_duplicate = self.canonical_snapshot(created["id"])
            self.require(
                before_duplicate["sha256"] == after_duplicate["sha256"],
                "duplicate delivery changed cancelled Run evidence",
                {
                    "before": before_duplicate["sha256"],
                    "after": after_duplicate["sha256"],
                },
            )
            entry["evidence"] = {
                "run_id": created["id"],
                "barrier": {
                    "completed_questions": partial["completed_questions"],
                    "lease_owner": partial["lease_owner"],
                    "lease_token": partial["lease_token"],
                },
                "cancelled": {field: final.get(field) for field in RUN_SNAPSHOT_FIELDS},
                "protocol_snapshot": before_duplicate,
                "duplicate_message_id": duplicate_id,
                "snapshot_sha256_after_duplicate": after_duplicate["sha256"],
                "queue_after_ack": queue,
            }

    def migration_round_trip_scenario(self) -> None:
        with self.scenario(
            "postgres_populated_0005_and_0004_downgrade_guards_with_empty_round_trips"
        ) as entry:
            self.require(self.baseline_run_id is not None, "baseline Run is unavailable")
            run_id = self.baseline_run_id
            queue_before = self.wait_queue_drained(timeout=60)
            active_raw = self.psql(
                "SELECT count(*) FROM evaluation_runs WHERE status IN ('pending', 'running');"
            ).stdout.strip()
            self.require(active_raw == "0", "migration round trip found active Runs", active_raw)
            core_before = self.db_core_protocol_snapshot(run_id)
            reliability_before = self.db_run_snapshot(run_id)
            application_counts = json.loads(
                self.psql(
                    "SELECT json_build_object("
                    "'policies', (SELECT count(*) FROM governance_policies), "
                    "'reservations', (SELECT count(*) FROM provider_call_reservations), "
                    "'audit_events', (SELECT count(*) FROM audit_events), "
                    "'worker_processes', (SELECT count(*) FROM worker_processes))::text;"
                ).stdout.strip()
            )
            self.require(
                application_counts["worker_processes"] > 0,
                "0005 downgrade refusal requires persisted Worker process facts",
                application_counts,
            )
            self.require(
                application_counts["policies"] > 0
                and application_counts["reservations"] > 0
                and application_counts["audit_events"] > 0,
                "0004 downgrade refusal requires populated governance evidence",
                application_counts,
            )

            self.compose("stop", "api", "worker", timeout=90)
            stopped_api = self.service_metas("api", include_stopped=True)
            stopped_workers = self.service_metas("worker", include_stopped=True)
            self.require(
                len(stopped_api) == 1 and stopped_api[0]["status"] == "exited",
                "API did not stop before migration downgrade",
                stopped_api,
            )
            self.require(
                len(stopped_workers) == 2
                and all(worker["status"] == "exited" for worker in stopped_workers),
                "Workers did not stop before migration downgrade",
                stopped_workers,
            )

            worker_progress_downgrade = self.compose(
                "run",
                "--rm",
                "--no-deps",
                "migrate",
                "alembic",
                "downgrade",
                GOVERNANCE_REVISION,
                timeout=180,
                check=False,
            )
            self.require(
                worker_progress_downgrade.returncode != 0,
                "populated 0005 downgrade unexpectedly discarded Worker progress evidence",
            )
            worker_progress_refusal_output = "\n".join(
                part
                for part in (
                    worker_progress_downgrade.stdout,
                    worker_progress_downgrade.stderr,
                )
                if part
            )
            self.require(
                "Cannot downgrade Worker progress schema" in worker_progress_refusal_output,
                "populated 0005 downgrade did not return the stable refusal reason",
                redact_text(worker_progress_refusal_output[-2000:]),
            )
            application_revision = self.psql(
                "SELECT version_num FROM alembic_version;"
            ).stdout.strip()
            self.require(
                application_revision == DATABASE_HEAD_REVISION,
                "failed populated downgrade changed the application database revision",
                application_revision,
            )
            core_after_refusal = self.db_core_protocol_snapshot(run_id)
            reliability_after_refusal = self.db_run_snapshot(run_id)

            governance_database = "p2governance_" + self.project[-12:]
            governance_database_url = (
                f"postgresql+psycopg://llmbenchlab:{LOCAL_PASSWORD}@postgres:5432/"
                f"{governance_database}?connect_timeout=3"
            )
            governance_guard: dict[str, Any] = {
                "database": governance_database,
                "source_application_revision": application_revision,
                "worker_facts_cleared_in_clone_only": True,
            }
            governance_guard["safe_template_clone"] = self.clone_application_database(
                governance_database
            )
            try:
                self.psql_database(
                    "TRUNCATE TABLE worker_processes;",
                    database=governance_database,
                )
                prepare_governance = self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    f"DATABASE_URL={governance_database_url}",
                    "migrate",
                    "alembic",
                    "downgrade",
                    GOVERNANCE_REVISION,
                    timeout=180,
                )
                governance_revision_before = self.psql_database(
                    "SELECT version_num FROM alembic_version;",
                    database=governance_database,
                ).stdout.strip()
                self.require(
                    governance_revision_before == GOVERNANCE_REVISION,
                    "isolated populated database did not prepare revision 0004",
                    governance_revision_before,
                )
                governance_counts_before = json.loads(
                    self.psql_database(
                        "SELECT json_build_object("
                        "'policies', (SELECT count(*) FROM governance_policies), "
                        "'reservations', (SELECT count(*) FROM provider_call_reservations), "
                        "'audit_events', (SELECT count(*) FROM audit_events))::text;",
                        database=governance_database,
                    ).stdout.strip()
                )
                self.require(
                    governance_counts_before["policies"] > 0
                    and governance_counts_before["reservations"] > 0
                    and governance_counts_before["audit_events"] > 0,
                    "isolated 0004 downgrade guard lacks populated governance evidence",
                    governance_counts_before,
                )
                governance_downgrade = self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    f"DATABASE_URL={governance_database_url}",
                    "migrate",
                    "alembic",
                    "downgrade",
                    PRE_GOVERNANCE_REVISION,
                    timeout=180,
                    check=False,
                )
                self.require(
                    governance_downgrade.returncode != 0,
                    "populated 0004 downgrade unexpectedly discarded governance evidence",
                )
                governance_refusal_output = "\n".join(
                    part
                    for part in (governance_downgrade.stdout, governance_downgrade.stderr)
                    if part
                )
                self.require(
                    "Cannot downgrade governance schema" in governance_refusal_output,
                    "populated 0004 downgrade did not return the stable refusal reason",
                    redact_text(governance_refusal_output[-2000:]),
                )
                governance_revision_after = self.psql_database(
                    "SELECT version_num FROM alembic_version;",
                    database=governance_database,
                ).stdout.strip()
                self.require(
                    governance_revision_after == GOVERNANCE_REVISION,
                    "failed populated 0004 downgrade changed the isolated database revision",
                    governance_revision_after,
                )
                governance_counts_after = json.loads(
                    self.psql_database(
                        "SELECT json_build_object("
                        "'policies', (SELECT count(*) FROM governance_policies), "
                        "'reservations', (SELECT count(*) FROM provider_call_reservations), "
                        "'audit_events', (SELECT count(*) FROM audit_events))::text;",
                        database=governance_database,
                    ).stdout.strip()
                )
                self.require(
                    governance_counts_after == governance_counts_before,
                    "refused populated 0004 downgrade changed governance evidence",
                    {
                        "before": governance_counts_before,
                        "after": governance_counts_after,
                    },
                )
                governance_guard.update(
                    {
                        "prepare_0004_returncode": prepare_governance.returncode,
                        "revision_before_refusal": governance_revision_before,
                        "populated_counts_before": governance_counts_before,
                        "downgrade_to_0003_returncode": governance_downgrade.returncode,
                        "refusal_reason": "Cannot downgrade governance schema",
                        "revision_after_refusal": governance_revision_after,
                        "populated_counts_after": governance_counts_after,
                    }
                )
            finally:
                drop_governance = self.psql_database(
                    f'DROP DATABASE IF EXISTS "{governance_database}" WITH (FORCE);',
                    database="postgres",
                    check=False,
                )
                governance_guard["drop_returncode"] = drop_governance.returncode
            self.require(
                governance_guard["drop_returncode"] == 0,
                "isolated populated governance database cleanup failed",
                governance_guard,
            )
            application_worker_facts_after = int(
                self.psql("SELECT count(*) FROM worker_processes;").stdout.strip()
            )
            self.require(
                application_worker_facts_after == application_counts["worker_processes"],
                "isolated governance guard changed application Worker process facts",
                {
                    "before": application_counts["worker_processes"],
                    "after": application_worker_facts_after,
                },
            )

            empty_database = "p2roundtrip_" + self.project[-12:]
            empty_database_url = (
                f"postgresql+psycopg://llmbenchlab:{LOCAL_PASSWORD}@postgres:5432/"
                f"{empty_database}?connect_timeout=3"
            )
            empty_round_trips: dict[str, Any] = {"database": empty_database}
            self.psql_database(
                f'CREATE DATABASE "{empty_database}";',
                database="postgres",
            )
            try:
                upgrade_empty = self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    f"DATABASE_URL={empty_database_url}",
                    "migrate",
                    "alembic",
                    "upgrade",
                    "head",
                    timeout=180,
                )
                empty_head_before = self.psql_database(
                    "SELECT version_num FROM alembic_version;",
                    database=empty_database,
                ).stdout.strip()
                self.require(
                    empty_head_before == DATABASE_HEAD_REVISION,
                    "empty PostgreSQL database did not upgrade to current head",
                    empty_head_before,
                )
                downgrade_empty = self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    f"DATABASE_URL={empty_database_url}",
                    "migrate",
                    "alembic",
                    "downgrade",
                    GOVERNANCE_REVISION,
                    timeout=180,
                )
                empty_pre_worker_progress = self.psql_database(
                    "SELECT version_num FROM alembic_version;",
                    database=empty_database,
                ).stdout.strip()
                self.require(
                    empty_pre_worker_progress == GOVERNANCE_REVISION,
                    "empty PostgreSQL database did not downgrade across 0005",
                    empty_pre_worker_progress,
                )
                reupgrade_worker_progress = self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    f"DATABASE_URL={empty_database_url}",
                    "migrate",
                    "alembic",
                    "upgrade",
                    WORKER_PROGRESS_REVISION,
                    timeout=180,
                )
                worker_progress_head_after = self.psql_database(
                    "SELECT version_num FROM alembic_version;",
                    database=empty_database,
                ).stdout.strip()
                self.require(
                    worker_progress_head_after == WORKER_PROGRESS_REVISION,
                    "empty PostgreSQL database did not complete the 0005 round trip",
                    worker_progress_head_after,
                )

                prepare_empty_governance = self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    f"DATABASE_URL={empty_database_url}",
                    "migrate",
                    "alembic",
                    "downgrade",
                    GOVERNANCE_REVISION,
                    timeout=180,
                )
                empty_governance_before = self.psql_database(
                    "SELECT version_num FROM alembic_version;",
                    database=empty_database,
                ).stdout.strip()
                self.require(
                    empty_governance_before == GOVERNANCE_REVISION,
                    "empty PostgreSQL database did not prepare revision 0004",
                    empty_governance_before,
                )
                downgrade_empty_governance = self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    f"DATABASE_URL={empty_database_url}",
                    "migrate",
                    "alembic",
                    "downgrade",
                    PRE_GOVERNANCE_REVISION,
                    timeout=180,
                )
                empty_pre_governance = self.psql_database(
                    "SELECT version_num FROM alembic_version;",
                    database=empty_database,
                ).stdout.strip()
                self.require(
                    empty_pre_governance == PRE_GOVERNANCE_REVISION,
                    "empty PostgreSQL database did not downgrade across 0004",
                    empty_pre_governance,
                )
                reupgrade_empty_governance = self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    f"DATABASE_URL={empty_database_url}",
                    "migrate",
                    "alembic",
                    "upgrade",
                    GOVERNANCE_REVISION,
                    timeout=180,
                )
                governance_head_after = self.psql_database(
                    "SELECT version_num FROM alembic_version;",
                    database=empty_database,
                ).stdout.strip()
                self.require(
                    governance_head_after == GOVERNANCE_REVISION,
                    "empty PostgreSQL database did not complete the 0004 round trip",
                    governance_head_after,
                )
                final_reupgrade_and_check = self.compose(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    f"DATABASE_URL={empty_database_url}",
                    "migrate",
                    "sh",
                    "-c",
                    "alembic upgrade head && alembic check",
                    timeout=180,
                )
                empty_final_head = self.psql_database(
                    "SELECT version_num FROM alembic_version;",
                    database=empty_database,
                ).stdout.strip()
                self.require(
                    empty_final_head == DATABASE_HEAD_REVISION,
                    "empty PostgreSQL database did not return to migration head",
                    empty_final_head,
                )
                empty_round_trips.update(
                    {
                        "initial_upgrade_returncode": upgrade_empty.returncode,
                        "initial_head": empty_head_before,
                        "worker_progress_0005_to_0004_round_trip": {
                            "downgrade_returncode": downgrade_empty.returncode,
                            "pre_worker_progress_revision": empty_pre_worker_progress,
                            "reupgrade_returncode": reupgrade_worker_progress.returncode,
                            "head_after_reupgrade": worker_progress_head_after,
                        },
                        "governance_0004_to_0003_round_trip": {
                            "prepare_0004_returncode": prepare_empty_governance.returncode,
                            "head_before": empty_governance_before,
                            "downgrade_returncode": downgrade_empty_governance.returncode,
                            "pre_governance_revision": empty_pre_governance,
                            "reupgrade_returncode": reupgrade_empty_governance.returncode,
                            "head_after_reupgrade": governance_head_after,
                        },
                        "final_reupgrade_and_check_returncode": (
                            final_reupgrade_and_check.returncode
                        ),
                        "final_head": empty_final_head,
                    }
                )
            finally:
                drop_empty = self.psql_database(
                    f'DROP DATABASE IF EXISTS "{empty_database}" WITH (FORCE);',
                    database="postgres",
                    check=False,
                )
                empty_round_trips["drop_returncode"] = drop_empty.returncode
            self.require(
                empty_round_trips["drop_returncode"] == 0,
                "isolated empty migration database cleanup failed",
                empty_round_trips,
            )

            hashes = {
                "before": core_before["sha256"],
                "after_refused_downgrade": core_after_refusal["sha256"],
            }
            entry["evidence"] = {
                "run_id": run_id,
                "queue_before": queue_before,
                "active_runs_before": 0,
                "api_stopped": stopped_api,
                "workers_stopped": stopped_workers,
                "application_worker_progress_0005_to_0004_guard": {
                    "populated_counts": application_counts,
                    "downgrade_returncode": worker_progress_downgrade.returncode,
                    "refusal_reason": "Cannot downgrade Worker progress schema",
                    "revision_after_refusal": application_revision,
                    "worker_facts_after_isolated_governance_guard": (
                        application_worker_facts_after
                    ),
                },
                "isolated_governance_0004_to_0003_guard": governance_guard,
                "empty_database_round_trips": empty_round_trips,
                "core_protocol_hashes": hashes,
                "core_snapshot_before": core_before,
                "reliability_before": reliability_before,
                "reliability_after_refusal": reliability_after_refusal,
            }
            self.write_evidence()
            self.require(
                len(set(hashes.values())) == 1,
                "core protocol/Response evidence changed after refused populated downgrade",
                hashes,
            )

            self.start_validated_containers(stopped_api, expected_service="api")
            self.start_validated_containers(stopped_workers, expected_service="worker")
            api_healthy = self.wait_service_healthy("api", timeout=90)
            workers_healthy = self.wait_service_healthy("worker", count=2, timeout=90)
            ready = self.wait_api_ready(200, timeout=45)
            protocol_after_restart = self.assert_complete_protocol(run_id, expected_attempts=None)
            entry["evidence"].update(
                {
                    "api_after_restart": api_healthy,
                    "workers_after_restart": workers_healthy,
                    "ready_after_restart": ready["payload"],
                    "api_protocol_snapshot_after_restart": protocol_after_restart,
                }
            )

    def run_all(self) -> None:
        self.evidence["status"] = "running"
        self.write_evidence()
        self.setup_stack()
        self.topology_scenario()
        self.baseline_scenario()
        self.api_restart_scenario()
        self.worker_crash_scenario()
        self.database_crash_seams_scenario()
        self.redis_outage_scenario()
        self.pending_cancel_scenario()
        self.running_cancel_scenario()
        self.migration_round_trip_scenario()
        final_queue = self.wait_queue_drained(timeout=60)
        self.evidence["final_invariants"] = {
            "queue": final_queue,
            "workers": self.wait_service_healthy("worker", count=2, timeout=60),
            "api_ready": self.wait_api_ready(200, timeout=30)["payload"],
        }

    def collect_diagnostics(self) -> None:
        if not self.stack_touched:
            return
        diagnostics: Dict[str, Any] = {"captured_at": utc_now()}
        ps = self.compose("ps", "-a", "--format", "json", timeout=30, check=False, record=False)
        diagnostics["compose_ps"] = redact_text(ps.stdout or ps.stderr)
        logs = self.compose(
            "logs",
            "--no-color",
            "--timestamps",
            "postgres",
            "redis",
            "migrate",
            "api",
            "worker",
            "frontend",
            timeout=60,
            check=False,
            record=False,
            max_recorded_chars=0,
        )
        combined_logs = (logs.stdout + "\n" + logs.stderr)[-250000:]
        diagnostics["service_logs_tail"] = redact_text(combined_logs)
        runs = self.psql(
            "SELECT COALESCE(json_agg(json_build_object("
            "'id', id, 'status', status, 'attempt_count', attempt_count, "
            "'lease_token', lease_token, 'lease_owner', lease_owner, "
            "'completed_questions', completed_questions, 'last_error', last_error) "
            "ORDER BY created_at), '[]'::json)::text FROM evaluation_runs;",
            check=False,
        )
        if runs.returncode == 0 and runs.stdout.strip():
            try:
                diagnostics["database_runs"] = json.loads(runs.stdout.strip())
            except json.JSONDecodeError:
                diagnostics["database_runs"] = redact_text(runs.stdout)
        redis_pending = self.redis_cli("XPENDING", TASK_STREAM, TASK_GROUP, check=False)
        diagnostics["redis_xpending"] = redact_text(redis_pending.stdout or redis_pending.stderr)
        self.evidence["diagnostics"] = diagnostics

    def cleanup(self) -> Optional[str]:
        if not self.stack_touched:
            self.evidence["cleanup"] = {
                "status": "not_needed",
                "finished_at": utc_now(),
            }
            return None
        if PROJECT_PATTERN.fullmatch(self.project) is None:
            return "refused cleanup because the project name failed its safety guard"

        down = self.compose(
            "down",
            "-v",
            "--remove-orphans",
            timeout=180,
            check=False,
        )
        remaining_containers = self.compose(
            "ps", "-a", "-q", timeout=20, check=False
        ).stdout.split()
        volumes = self.run_command(
            [
                "docker",
                "volume",
                "ls",
                "--filter",
                "label=com.docker.compose.project={}".format(self.project),
                "-q",
            ],
            timeout=20,
            check=False,
        ).stdout.split()
        networks = self.run_command(
            [
                "docker",
                "network",
                "ls",
                "--filter",
                "label=com.docker.compose.project={}".format(self.project),
                "-q",
            ],
            timeout=20,
            check=False,
        ).stdout.split()
        cleanup_ok = (
            down.returncode == 0 and not remaining_containers and not volumes and not networks
        )
        self.evidence["cleanup"] = {
            "status": "passed" if cleanup_ok else "failed",
            "finished_at": utc_now(),
            "down_returncode": down.returncode,
            "remaining_containers": remaining_containers,
            "remaining_project_volumes": volumes,
            "remaining_project_networks": networks,
        }
        if cleanup_ok:
            return None
        return "isolated Compose project cleanup was incomplete"


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=DEFAULT_ARTIFACTS_ROOT,
        help="gitignored repository-relative evidence root",
    )
    parser.add_argument(
        "--self-check-only",
        action="store_true",
        help="validate isolation and Compose configuration without creating containers",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_arguments(argv)
    repository_root = Path(__file__).resolve().parents[1]
    harness = Phase2Acceptance(repository_root, args.artifacts_root)
    exit_code = 1

    def interrupt(_signum: int, _frame: Any) -> None:
        raise AcceptanceInterrupted("received termination signal")

    previous_term = signal.signal(signal.SIGTERM, interrupt)
    try:
        review = harness.self_review()
        if args.self_check_only:
            print(json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        harness.write_evidence()
        print("Phase 2 evidence: {}".format(harness.evidence_path))
        harness.run_all()
        harness.evidence["status"] = "passed"
        exit_code = 0
    except (AcceptanceFailure, AcceptanceInterrupted, KeyboardInterrupt) as exc:
        harness.evidence["status"] = "failed"
        harness.evidence["failure"] = {
            "type": type(exc).__name__,
            "message": redact_text(str(exc)),
            "traceback": redact_text(traceback.format_exc()),
        }
        print("Phase 2 acceptance failed: {}".format(redact_text(str(exc))), file=sys.stderr)
    except BaseException as exc:
        harness.evidence["status"] = "failed"
        harness.evidence["failure"] = {
            "type": type(exc).__name__,
            "message": redact_text(str(exc)),
            "traceback": redact_text(traceback.format_exc()),
        }
        print("Unexpected acceptance failure: {}".format(redact_text(str(exc))), file=sys.stderr)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        if not args.self_check_only:
            try:
                harness.collect_diagnostics()
            except BaseException as exc:
                harness.evidence["diagnostics_error"] = redact_text(str(exc))
                if exit_code == 0:
                    harness.evidence["status"] = "failed"
                    exit_code = 1
            try:
                cleanup_error = harness.cleanup()
            except BaseException as exc:
                cleanup_error = "cleanup raised {}: {}".format(
                    type(exc).__name__, redact_text(str(exc))
                )
            if cleanup_error is not None:
                harness.evidence["status"] = "failed"
                harness.evidence["cleanup_error"] = cleanup_error
                exit_code = 1
            harness.evidence["finished_at"] = utc_now()
            try:
                harness.write_evidence()
            except BaseException as exc:
                print("Could not write evidence: {}".format(exc), file=sys.stderr)
                exit_code = 1

    if not args.self_check_only:
        print("Phase 2 acceptance status: {}".format(harness.evidence["status"]))
        print("Evidence retained at: {}".format(harness.evidence_path))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
