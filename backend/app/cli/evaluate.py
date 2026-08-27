"""Prepare a pinned benchmark and run it through a real compatible API.

The command is deliberately local-only.  A Provider key is read from an
environment variable or a hidden terminal prompt and remains outside argv,
REST requests, persistence, and exported reports.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, is_dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import sanitize_error_message
from app.core.config import Settings, get_settings
from app.core.constants import DEFAULT_MAX_RETRIES
from app.core.logging import configure_logging
from app.core.time import utc_now
from app.db.init_db import initialize_database
from app.db.session import SessionLocal, engine
from app.models import EvaluationRun, Model, ProviderType, RunStatus
from app.providers import (
    CanaryResult,
    ModelDiscoveryResult,
    ProviderPreflightError,
    discover_models,
    run_chat_canary,
    select_remote_model,
)
from app.reports import GROUP_FIELD_WHITELIST, ReportExportError, export_run_report
from app.runners.evaluation_runner import EvaluationRunner
from app.runners.run_leases import RunLeaseRepository
from app.schemas.evaluation_run import EvaluationRunCreate
from app.schemas.model import ENV_VAR_PATTERN, ModelCreate
from app.services.benchmark_service import persist_dataset
from app.services.run_service import build_evaluation_run
from app.standard_datasets import prepare_gpqa_diamond, prepare_mmlu_pro

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_DIR = PROJECT_ROOT / "artifacts" / "dataset-cache"
DEFAULT_BENCHMARK_DIR = PROJECT_ROOT / "artifacts" / "benchmarks"
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "artifacts" / "evaluations"
DEFAULT_API_KEY_ENV = "LLMBENCHLAB_REAL_API_KEY"
TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


class EvaluationCLIError(RuntimeError):
    """A safe operator-facing CLI error."""


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _price(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise argparse.ArgumentTypeError("price must be a decimal number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise argparse.ArgumentTypeError("price must be finite and non-negative")
    return parsed


def _csv_values(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    values = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    return values or None


def _add_dataset_arguments(parser: argparse.ArgumentParser, *, require_scope: bool) -> None:
    parser.add_argument("--dataset", choices=("mmlu-pro", "gpqa-diamond"), required=True)
    scope = parser.add_mutually_exclusive_group(required=require_scope)
    scope.add_argument(
        "--full",
        action="store_true",
        help="Prepare every selected question. Required for a full benchmark result.",
    )
    scope.add_argument(
        "--limit",
        type=_positive_int,
        help="Prepare only the first N deterministically selected questions.",
    )
    parser.add_argument(
        "--groups",
        help="Comma-separated MMLU categories or GPQA high-level domains.",
    )
    parser.add_argument(
        "--profile",
        choices=("official_cot", "direct"),
        default="official_cot",
        help="MMLU-Pro prompt profile; ignored by GPQA-Diamond.",
    )
    parser.add_argument(
        "--shuffle-seed",
        type=int,
        default=42,
        help="GPQA per-record deterministic option-shuffle seed.",
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--benchmark-dir", type=Path, default=DEFAULT_BENCHMARK_DIR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmbenchlab-evaluate",
        allow_abbrev=False,
        description=(
            "Pinned standard-dataset evaluation for a trusted local OpenAI-compatible API. "
            "API keys are never accepted as command-line arguments."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        allow_abbrev=False,
        help="Download, verify, and convert a standard dataset without a Provider.",
    )
    _add_dataset_arguments(prepare, require_scope=False)

    run = subparsers.add_parser(
        "run",
        allow_abbrev=False,
        help="Prepare a benchmark, preflight a Provider, and execute a new Run.",
    )
    _add_dataset_arguments(run, require_scope=True)
    run.add_argument("--base-url", required=True)
    run.add_argument(
        "--model", help="Remote model ID; auto-selected only when discovery is unique."
    )
    run.add_argument("--display-name", help="Local Model display name.")
    run.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    run.add_argument(
        "--no-model-discovery",
        action="store_true",
        help="Skip GET /models; requires --model. The billed Chat canary still runs.",
    )
    run.add_argument("--temperature", type=float)
    run.add_argument("--top-p", type=float, default=1.0)
    run.add_argument("--max-tokens", type=_positive_int)
    run.add_argument("--no-seed", action="store_true")
    run.add_argument("--generation-seed", type=int, default=42)
    run.add_argument("--concurrency", type=int, choices=(1, 2, 3, 4), default=1)
    run.add_argument("--input-price-per-million", type=_price)
    run.add_argument("--output-price-per-million", type=_price)
    run.add_argument("--report-dir", type=Path)
    run.add_argument(
        "--yes",
        action="store_true",
        help="Confirm non-interactively after the command prints the request upper bound.",
    )

    resume = subparsers.add_parser(
        "resume",
        allow_abbrev=False,
        help="Resume missing responses for an existing non-terminal Run.",
    )
    resume.add_argument("run_id")
    resume.add_argument(
        "--api-key-env",
        help="Optional source env var; copied temporarily into the Run's frozen key env name.",
    )
    resume.add_argument("--no-model-discovery", action="store_true")
    resume.add_argument("--report-dir", type=Path)
    resume.add_argument("--yes", action="store_true")

    report = subparsers.add_parser(
        "report",
        allow_abbrev=False,
        help="Export all currently persisted evidence without calling a Provider.",
    )
    report.add_argument("run_id")
    report.add_argument("--output-dir", type=Path)
    report.add_argument(
        "--group-by",
        choices=GROUP_FIELD_WHITELIST,
    )
    return parser


def _loaded_dataset(prepared: object) -> Any:
    for field in ("loaded_dataset", "dataset"):
        value = getattr(prepared, field, None)
        if value is not None:
            return value
    raise EvaluationCLIError("Dataset plugin returned no LoadedDataset.")


def _archive_path(prepared: object) -> Path:
    value = getattr(prepared, "archive_path", None)
    if value is None:
        raise EvaluationCLIError("Dataset plugin returned no archive path.")
    return Path(value)


def _prepare_dataset(args: argparse.Namespace) -> object:
    groups = _csv_values(getattr(args, "groups", None))
    limit = getattr(args, "limit", None)
    args.cache_dir = args.cache_dir.expanduser().resolve()
    args.benchmark_dir = args.benchmark_dir.expanduser().resolve()
    if args.dataset == "mmlu-pro":
        return prepare_mmlu_pro(
            args.benchmark_dir,
            cache_dir=args.cache_dir,
            profile=args.profile,
            categories=groups,
            limit=limit,
        )
    return prepare_gpqa_diamond(
        args.benchmark_dir,
        cache_dir=args.cache_dir,
        seed=args.shuffle_seed,
        domains=groups,
        limit=limit,
    )


def _prepared_summary(prepared: object) -> dict[str, Any]:
    dataset = _loaded_dataset(prepared)
    summary = {
        "archive_path": str(_archive_path(prepared)),
        "archive_sha256": getattr(prepared, "archive_sha256", None),
        "dataset_hash": dataset.dataset_hash,
        "slug": dataset.manifest["id"],
        "version": dataset.manifest["version"],
        "question_count": dataset.manifest["question_count"],
        "source": dataset.manifest["source"],
        "license": dataset.manifest["license"],
    }
    source = getattr(prepared, "source", None) or getattr(prepared, "source_metadata", None)
    if is_dataclass(source):
        summary["source_evidence"] = asdict(source)
    elif isinstance(source, dict):
        summary["source_evidence"] = source
    return summary


@contextmanager
def _secret_environment(
    target_env: str,
    *,
    source_env: str | None = None,
) -> Iterator[str]:
    source_name = source_env or target_env
    if not ENV_VAR_PATTERN.fullmatch(target_env) or not ENV_VAR_PATTERN.fullmatch(source_name):
        raise EvaluationCLIError("API key environment variable name is invalid.")
    value = os.environ.get(source_name)
    if not value:
        if not sys.stdin.isatty():
            raise EvaluationCLIError(
                f"Environment variable {source_name!r} is empty and no interactive terminal "
                "is available."
            )
        value = getpass.getpass(f"API key for {target_env} (input hidden): ")
    if not value:
        raise EvaluationCLIError("API key is empty.")

    existed = target_env in os.environ
    previous = os.environ.get(target_env)
    os.environ[target_env] = value
    try:
        yield value
    finally:
        if existed and previous is not None:
            os.environ[target_env] = previous
        else:
            os.environ.pop(target_env, None)


def _provider_defaults(args: argparse.Namespace) -> dict[str, Any]:
    official_cot = args.dataset == "mmlu-pro" and args.profile == "official_cot"
    temperature = args.temperature if args.temperature is not None else 0
    max_tokens = (
        args.max_tokens if args.max_tokens is not None else (4000 if official_cot else 1024)
    )
    candidate = EvaluationRunCreate(
        model_id="preflight-model",
        benchmark_id="preflight-benchmark",
        temperature=temperature,
        top_p=args.top_p,
        max_tokens=max_tokens,
        seed=None if args.no_seed else args.generation_seed,
        concurrency=args.concurrency,
    )
    return {
        field: getattr(candidate, field)
        for field in ("temperature", "top_p", "max_tokens", "seed", "concurrency")
    }


async def _resolve_and_canary(
    *,
    base_url: str,
    requested_model: str | None,
    api_key: str,
    api_key_env: str,
    generation: dict[str, Any],
    skip_discovery: bool,
    question_count: int,
    run_attempts: int,
    yes: bool,
    before_canary: Callable[[str], None] | None = None,
) -> tuple[str, ModelDiscoveryResult | None, CanaryResult]:
    if skip_discovery and not requested_model:
        raise EvaluationCLIError("--no-model-discovery requires --model.")
    if api_key in base_url or (requested_model is not None and api_key in requested_model):
        raise EvaluationCLIError(
            "Provider configuration contains the API key and was rejected before network access."
        )

    discovery: ModelDiscoveryResult | None = None
    if not skip_discovery:
        try:
            discovery = await discover_models(base_url, api_key)
        except ProviderPreflightError as exc:
            if exc.code == "model_discovery_unsupported" and requested_model:
                print(
                    "Notice: Provider does not expose GET /models; continuing with the "
                    "explicit model.",
                    file=sys.stderr,
                )
            else:
                raise
    remote_model = select_remote_model(
        requested_model,
        discovery.models if discovery is not None else None,
    )
    if api_key in remote_model:
        raise EvaluationCLIError("Provider returned an unsafe model identifier.")
    if before_canary is not None:
        before_canary(remote_model)
    _confirm_real_calls(
        remote_model,
        base_url,
        question_count,
        run_attempts=run_attempts,
        yes=yes,
    )
    canary = await run_chat_canary(
        base_url,
        remote_model,
        api_key_env,
        generation,
    )
    return remote_model, discovery, canary


def _confirm_real_calls(
    remote_model: str,
    base_url: str,
    question_count: int,
    *,
    run_attempts: int,
    yes: bool,
) -> None:
    if run_attempts < 1:
        raise EvaluationCLIError("The Run has no remaining execution attempts.")
    adapter_attempts = DEFAULT_MAX_RETRIES + 1
    maximum_requests = (question_count * run_attempts + 1) * adapter_attempts
    host = urlsplit(base_url).hostname or "unknown-host"
    message = (
        f"Provider host: {host}\n"
        f"Remote model: {remote_model}\n"
        f"Scored questions: {question_count}\n"
        f"Remaining Run attempts included in upper bound: {run_attempts}\n"
        f"Maximum billed Chat Completion attempts this invocation: {maximum_requests} "
        f"(one canary plus up to {adapter_attempts} HTTP attempts per question per Run attempt)\n"
        "Pricing or usage may be unknown; LLMBenchLab cannot enforce a global Provider "
        "budget yet.\n"
        "Operational prerequisite: stop the regular API/Worker stack so this CLI exclusively "
        "owns the evaluation database."
    )
    print(message, file=sys.stderr)
    if yes:
        return
    if not sys.stdin.isatty():
        raise EvaluationCLIError("Interactive confirmation is unavailable; pass --yes to proceed.")
    confirmation = input("Type RUN to start the billed canary and evaluation: ").strip()
    if confirmation != "RUN":
        raise EvaluationCLIError("Evaluation was not confirmed; no billed canary was sent.")


def _model_name(base_url: str, remote_model: str, requested: str | None) -> str:
    if requested:
        normalized = requested.strip()
        if normalized:
            return normalized[:160]
    host = urlsplit(base_url).hostname or "provider"
    digest = hashlib.sha256(f"{base_url}\0{remote_model}".encode()).hexdigest()[:8]
    return f"{remote_model} @ {host} [{digest}]"[:160]


def _model_payload(
    *,
    base_url: str,
    remote_model: str,
    api_key_env: str,
    display_name: str | None,
    input_price: Decimal | None,
    output_price: Decimal | None,
) -> ModelCreate:
    return ModelCreate(
        name=_model_name(base_url, remote_model, display_name),
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        base_url=base_url,
        remote_model_name=remote_model,
        api_key_env=api_key_env,
        enabled=True,
        input_price_per_million=input_price,
        output_price_per_million=output_price,
        default_parameters={},
    )


def _same_model_configuration(existing: Model, payload: ModelCreate) -> bool:
    def normalized_price(value: object) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    return all(
        (
            existing.provider_type == payload.provider_type,
            existing.base_url == payload.base_url,
            existing.remote_model_name == payload.remote_model_name,
            existing.api_key_env == payload.api_key_env,
            existing.input_price_per_million == normalized_price(payload.input_price_per_million),
            existing.output_price_per_million == normalized_price(payload.output_price_per_million),
            dict(existing.default_parameters or {}) == payload.default_parameters,
        )
    )


def _resolve_model_registration(
    session: Session,
    payload: ModelCreate,
) -> tuple[Model | None, ModelCreate]:
    # Share the same Model row lock as REST Run creation and Model PATCH so a
    # CLI-created pending Run cannot race an endpoint/credential mutation.
    existing = session.scalar(select(Model).where(Model.name == payload.name).with_for_update())
    if existing is not None and _same_model_configuration(existing, payload):
        if not existing.enabled:
            raise EvaluationCLIError(
                f"Existing matching Model {existing.name!r} is disabled; enable it before running."
            )
        return existing, payload
    if existing is None:
        return None, payload

    base_name = payload.name
    fingerprint = hashlib.sha256(
        json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()[:8]
    payload.name = f"{base_name[:151]}-{fingerprint}"
    conflict = session.scalar(select(Model).where(Model.name == payload.name).with_for_update())
    if conflict is None:
        return None, payload
    if _same_model_configuration(conflict, payload):
        if not conflict.enabled:
            raise EvaluationCLIError(
                f"Existing matching Model {conflict.name!r} is disabled; enable it before running."
            )
        return conflict, payload
    raise EvaluationCLIError(
        "A Model name conflict remains after configuration fingerprinting. "
        "Pass a distinct --display-name."
    )


def _find_or_create_model(
    session: Session,
    *,
    base_url: str,
    remote_model: str,
    api_key_env: str,
    display_name: str | None,
    input_price: Decimal | None,
    output_price: Decimal | None,
) -> tuple[Model, bool]:
    payload = _model_payload(
        base_url=base_url,
        remote_model=remote_model,
        api_key_env=api_key_env,
        display_name=display_name,
        input_price=input_price,
        output_price=output_price,
    )
    existing, payload = _resolve_model_registration(session, payload)
    if existing is not None:
        return existing, False
    model = Model(
        **payload.model_dump(exclude={"api_key"}),
        credential_source="environment",
    )
    session.add(model)
    session.flush()
    return model, True


def _validate_model_registration(
    *,
    base_url: str,
    remote_model: str,
    api_key_env: str,
    display_name: str | None,
    input_price: Decimal | None,
    output_price: Decimal | None,
) -> None:
    payload = _model_payload(
        base_url=base_url,
        remote_model=remote_model,
        api_key_env=api_key_env,
        display_name=display_name,
        input_price=input_price,
        output_price=output_price,
    )
    with SessionLocal() as session:
        _resolve_model_registration(session, payload)


def _preflight_snapshot(
    discovery: ModelDiscoveryResult | None,
    canary: CanaryResult,
) -> dict[str, Any]:
    return {
        "performed_at": utc_now().isoformat(),
        "model_discovery": {
            "performed": discovery is not None,
            "model_count": len(discovery.models) if discovery is not None else None,
            "request_id": discovery.request_id if discovery is not None else None,
        },
        "chat_canary": {
            "status": "passed",
            "model": canary.model,
            "returned_model": canary.returned_model,
            "system_fingerprint": canary.system_fingerprint,
            "finish_reason": canary.finish_reason,
            "provider_request_id": canary.provider_request_id,
            "input_tokens": canary.input_tokens,
            "output_tokens": canary.output_tokens,
            "latency_ms": canary.latency_ms,
            "attempts": canary.attempts,
        },
    }


def _raise_if_run_is_active(session: Session) -> None:
    active_run = session.scalar(
        select(EvaluationRun.id)
        .where(EvaluationRun.status == RunStatus.RUNNING)
        .order_by(EvaluationRun.created_at)
        .limit(1)
    )
    if active_run is not None:
        raise EvaluationCLIError(
            f"Run {active_run} is already running. Stop the external Worker or wait for it "
            "before starting the trusted-local evaluator."
        )


def _ensure_no_active_run() -> None:
    with SessionLocal() as session:
        _raise_if_run_is_active(session)


def _create_persisted_run(
    settings: Settings,
    prepared: object,
    *,
    base_url: str,
    remote_model: str,
    api_key_env: str,
    display_name: str | None,
    input_price: Decimal | None,
    output_price: Decimal | None,
    generation: dict[str, Any],
    discovery: ModelDiscoveryResult | None,
    canary: CanaryResult,
) -> EvaluationRun:
    with SessionLocal() as session:
        _raise_if_run_is_active(session)
        benchmark, _created = persist_dataset(session, _loaded_dataset(prepared))
        model, _model_created = _find_or_create_model(
            session,
            base_url=base_url,
            remote_model=remote_model,
            api_key_env=api_key_env,
            display_name=display_name,
            input_price=input_price,
            output_price=output_price,
        )
        run_payload = EvaluationRunCreate(
            model_id=model.id,
            benchmark_id=benchmark.id,
            temperature=generation["temperature"],
            top_p=generation["top_p"],
            max_tokens=generation["max_tokens"],
            seed=generation["seed"],
            concurrency=generation["concurrency"],
        )
        run = build_evaluation_run(model, benchmark, run_payload, settings)
        snapshot = dict(run.model_parameters_snapshot)
        snapshot["preflight"] = _preflight_snapshot(discovery, canary)
        preparation = _prepared_summary(prepared)
        preparation.pop("archive_path", None)
        snapshot["dataset_preparation"] = preparation
        run.model_parameters_snapshot = snapshot
        session.add(run)
        session.commit()
        session.refresh(run)
        return run


def _load_run(run_id: str) -> EvaluationRun:
    with SessionLocal() as session:
        run = session.get(EvaluationRun, run_id)
        if run is None:
            raise EvaluationCLIError(f"Run {run_id!r} was not found.")
        session.expunge(run)
        return run


def _run_provider_configuration(run: EvaluationRun) -> tuple[str, str, str, dict[str, Any]]:
    snapshot = dict(run.model_parameters_snapshot or {})
    model = dict(snapshot.get("model", {}))
    generation = dict(snapshot.get("generation", {}))
    base_url = model.get("base_url")
    remote_model = model.get("remote_model_name")
    api_key_env = model.get("api_key_env")
    if not all(isinstance(value, str) and value for value in (base_url, remote_model, api_key_env)):
        raise EvaluationCLIError("Run does not contain a complete compatible Provider snapshot.")
    return str(base_url), str(remote_model), str(api_key_env), generation


async def _execute_with_progress(
    runner: EvaluationRunner,
    run_id: str,
    stop: asyncio.Event,
) -> bool:
    task = asyncio.create_task(
        runner.execute(run_id, shutdown_requested=stop),
        name=f"cli-run-{run_id}",
    )
    while not task.done():
        done, _pending = await asyncio.wait({task}, timeout=5)
        if done:
            break
        current = _load_run(run_id)
        print(
            f"Run {run_id}: {current.completed_questions}/{current.total_questions} "
            f"responses, status={current.status.value}",
            file=sys.stderr,
        )
    return await task


async def _drive_run(run_id: str, settings: Settings) -> EvaluationRun:
    repository = RunLeaseRepository(
        SessionLocal,
        lease_for=timedelta(seconds=settings.worker_lease_seconds),
        retry_backoff_base=timedelta(seconds=settings.worker_retry_backoff_base_seconds),
        retry_backoff_cap=timedelta(seconds=settings.worker_retry_backoff_cap_seconds),
    )
    runner = EvaluationRunner(SessionLocal, worker_id=f"trusted-local-cli:{os.getpid()}")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(signal_name)
    try:
        while True:
            run = _load_run(run_id)
            if run.status in TERMINAL_STATUSES or stop.is_set():
                return run
            if run.status == RunStatus.RUNNING:
                # Finalize terminal evidence first.  If an incomplete lease merely
                # expired, the reaper intentionally leaves it RUNNING so the next
                # claimant can fence and resume it; this trusted-local runner must
                # therefore attempt that claim instead of waiting forever for the
                # external Worker that the operator was required to stop.
                await asyncio.to_thread(repository.reap_expired)
                run = _load_run(run_id)
                if run.status in TERMINAL_STATUSES:
                    return run
                if run.status != RunStatus.RUNNING:
                    continue
                if run.lease_expires_at is None:
                    raise EvaluationCLIError(
                        f"Run {run.id} is running without a lease expiry; database state is "
                        "inconsistent."
                    )
                if run.lease_expires_at <= utc_now():
                    await _execute_with_progress(runner, run_id, stop)
                    continue
                remaining = max(0.0, (run.lease_expires_at - utc_now()).total_seconds())
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(),
                        timeout=min(remaining, settings.worker_poll_seconds, 1.0),
                    )
                continue
            if run.next_attempt_at is not None:
                delay = max(0.0, (run.next_attempt_at - utc_now()).total_seconds())
                if delay > 0:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=min(delay, 1.0))
                    continue
            await _execute_with_progress(runner, run_id, stop)
    finally:
        for signal_name in installed:
            loop.remove_signal_handler(signal_name)


def _report_directory(run_id: str, requested: Path | None) -> Path:
    return (requested or (DEFAULT_REPORT_ROOT / run_id)).expanduser().resolve()


def _export(
    run_id: str,
    output: Path,
    *,
    group_by: str | None = None,
    secret_values: Sequence[str] = (),
) -> Any:
    with SessionLocal() as session:
        return export_run_report(
            session,
            run_id,
            output,
            group_by=group_by,
            secret_values=secret_values,
        )


async def _run_new(args: argparse.Namespace, settings: Settings) -> int:
    await asyncio.to_thread(_ensure_no_active_run)
    prepared = await asyncio.to_thread(_prepare_dataset, args)
    prepared_summary = _prepared_summary(prepared)
    generation = _provider_defaults(args)
    validated_model = ModelCreate(
        name="validation-only",
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        base_url=args.base_url,
        remote_model_name=args.model or "pending-discovery",
        api_key_env=args.api_key_env,
    )
    base_url = str(validated_model.base_url)
    with _secret_environment(args.api_key_env) as api_key:
        remote_model, discovery, canary = await _resolve_and_canary(
            base_url=base_url,
            requested_model=args.model,
            api_key=api_key,
            api_key_env=args.api_key_env,
            generation=generation,
            skip_discovery=args.no_model_discovery,
            question_count=prepared_summary["question_count"],
            run_attempts=settings.worker_max_attempts,
            yes=args.yes,
            before_canary=lambda remote_model: _validate_model_registration(
                base_url=base_url,
                remote_model=remote_model,
                api_key_env=args.api_key_env,
                display_name=args.display_name,
                input_price=args.input_price_per_million,
                output_price=args.output_price_per_million,
            ),
        )
        run = _create_persisted_run(
            settings,
            prepared,
            base_url=base_url,
            remote_model=remote_model,
            api_key_env=args.api_key_env,
            display_name=args.display_name,
            input_price=args.input_price_per_million,
            output_price=args.output_price_per_million,
            generation=generation,
            discovery=discovery,
            canary=canary,
        )
        print(f"Run created: {run.id}", file=sys.stderr)
        terminal = await _drive_run(run.id, settings)

    if terminal.status in TERMINAL_STATUSES:
        report_dir = _report_directory(terminal.id, args.report_dir)
        exported = _export(terminal.id, report_dir, secret_values=(api_key,))
        print(
            json.dumps(
                {
                    "run_id": terminal.id,
                    "status": terminal.status.value,
                    "score": terminal.score,
                    "completion_rate": terminal.completion_rate,
                    "answered_accuracy": terminal.answered_accuracy,
                    "report_directory": str(exported.directory),
                    "benchmark": prepared_summary,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if terminal.status == RunStatus.COMPLETED else 4
    print(
        f"Run {terminal.id} stopped with status={terminal.status.value}. "
        f"Resume later with: llmbenchlab-evaluate resume {terminal.id}",
        file=sys.stderr,
    )
    return 3


async def _resume(args: argparse.Namespace, settings: Settings) -> int:
    run = _load_run(args.run_id)
    if run.status in TERMINAL_STATUSES:
        if run.status != RunStatus.COMPLETED:
            raise EvaluationCLIError(
                f"Run {run.id} is terminal ({run.status.value}) and cannot be resumed."
            )
        report_dir = _report_directory(run.id, args.report_dir)
        exported = _export(run.id, report_dir)
        print(
            json.dumps(
                {
                    "run_id": run.id,
                    "status": "completed",
                    "report_directory": str(exported.directory),
                },
                indent=2,
            )
        )
        return 0
    base_url, remote_model, target_env, generation = _run_provider_configuration(run)
    missing = max(0, run.total_questions - run.completed_questions)
    remaining_attempts = run.max_attempts - run.attempt_count
    if remaining_attempts < 1:
        raise EvaluationCLIError(
            f"Run {run.id} has exhausted all {run.max_attempts} execution attempts. "
            "Export its current evidence with the report command."
        )
    with _secret_environment(target_env, source_env=args.api_key_env) as api_key:
        _remote, _discovery, _canary = await _resolve_and_canary(
            base_url=base_url,
            requested_model=remote_model,
            api_key=api_key,
            api_key_env=target_env,
            generation=generation,
            skip_discovery=args.no_model_discovery,
            question_count=missing,
            run_attempts=remaining_attempts,
            yes=args.yes,
        )
        terminal = await _drive_run(run.id, settings)
    if terminal.status in TERMINAL_STATUSES:
        report_dir = _report_directory(terminal.id, args.report_dir)
        exported = _export(terminal.id, report_dir, secret_values=(api_key,))
        print(
            json.dumps(
                {
                    "run_id": terminal.id,
                    "status": terminal.status.value,
                    "score": terminal.score,
                    "report_directory": str(exported.directory),
                },
                indent=2,
            )
        )
        return 0 if terminal.status == RunStatus.COMPLETED else 4
    print(
        f"Run {terminal.id} remains non-terminal ({terminal.status.value}). "
        f"Resume later with: llmbenchlab-evaluate resume {terminal.id}",
        file=sys.stderr,
    )
    return 3


def _prepare_only(args: argparse.Namespace) -> int:
    prepared = _prepare_dataset(args)
    print(json.dumps(_prepared_summary(prepared), ensure_ascii=False, indent=2))
    return 0


def _report_only(args: argparse.Namespace) -> int:
    initialize_database()
    run = _load_run(args.run_id)
    output = _report_directory(run.id, args.output_dir)
    exported = _export(run.id, output, group_by=args.group_by)
    print(
        json.dumps(
            {
                "run_id": run.id,
                "status": run.status.value,
                "report_directory": str(exported.directory),
            },
            indent=2,
        )
    )
    return 0


async def _async_main(args: argparse.Namespace) -> int:
    if args.command == "prepare":
        return await asyncio.to_thread(_prepare_only, args)
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_database()
    if args.command == "run":
        return await _run_new(args, settings)
    if args.command == "resume":
        return await _resume(args, settings)
    raise AssertionError(f"unexpected async command: {args.command}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        status = _report_only(args) if args.command == "report" else asyncio.run(_async_main(args))
    except (
        EvaluationCLIError,
        ProviderPreflightError,
        ReportExportError,
        ValueError,
        OSError,
    ) as exc:
        print(
            "Error: " + sanitize_error_message(exc),
            file=sys.stderr,
        )
        status = 2
    finally:
        engine.dispose()
    raise SystemExit(status)


if __name__ == "__main__":
    main()
