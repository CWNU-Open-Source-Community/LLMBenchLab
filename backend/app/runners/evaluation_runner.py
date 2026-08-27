"""Fenced evaluation execution used only by the independent Worker."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters import AdapterError, build_adapter
from app.core.config import get_settings
from app.core.time import utc_now
from app.evaluators import get_evaluator
from app.models import (
    Benchmark,
    EvaluationResponse,
    EvaluationRun,
    ModelCredential,
    Question,
    RunStatus,
)
from app.runners.run_leases import (
    ResponseDisposition,
    RunLease,
    RunLeaseRepository,
    aggregate_run_evidence,
)
from app.security import CredentialKeyring, EncryptedCredential

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ModelSnapshot:
    provider_type: str
    base_url: str | None
    remote_model_name: str | None
    api_key_env: str | None
    input_price: Decimal | None
    output_price: Decimal | None
    credential_source: str = "none"
    api_key: SecretStr | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _QuestionSnapshot:
    id: str
    external_id: str
    question_type: str
    prompt: str
    choices: dict[str, str] | None
    reference_answer: Any
    evaluator_config: dict[str, Any]
    metadata: dict[str, Any]


class _LeaseUnavailable(RuntimeError):
    """Raised internally when a fenced Worker must stop processing a Run."""


class _ShutdownRequested(RuntimeError):
    """Raised after in-flight questions drain so the attempt can be resumed later."""


class EvaluationRunner:
    """Execute one claimed Run while isolating every question failure."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        worker_id: str | None = None,
        lease_repository: RunLeaseRepository | None = None,
    ) -> None:
        settings = get_settings()
        self._settings = settings
        self._session_factory = session_factory
        self._worker_id = worker_id or f"runner:{os.getpid()}:{uuid4()}"
        self._lease_repository = lease_repository or RunLeaseRepository(
            session_factory,
            lease_for=timedelta(seconds=settings.worker_lease_seconds),
            retry_backoff_base=timedelta(seconds=settings.worker_retry_backoff_base_seconds),
            retry_backoff_cap=timedelta(seconds=settings.worker_retry_backoff_cap_seconds),
        )
        self._heartbeat_seconds = settings.worker_heartbeat_seconds
        self._mock_generation_delay_seconds = settings.mock_generation_delay_seconds

    async def execute(
        self,
        run_id: str,
        *,
        shutdown_requested: asyncio.Event | None = None,
    ) -> bool:
        """Execute ``run_id`` and report whether a queue delivery is safe to ACK."""

        if shutdown_requested is not None and shutdown_requested.is_set():
            return False
        lease = self._claim(run_id)
        if lease is None:
            logger.info(
                "Run claim was a durable no-op",
                extra={
                    "event": "run_claim_noop",
                    "run_id": run_id,
                    "worker_id": self._worker_id,
                    "result": "not_claimable",
                },
            )
            return True
        logger.info(
            "Run lease claimed",
            extra={
                "event": "run_claimed",
                "run_id": run_id,
                "worker_id": lease.owner,
                "attempt": lease.attempt,
                "lease_token": lease.token,
                "result": "claimed",
            },
        )
        heartbeat_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat(lease, heartbeat_stop, lease_lost), name=f"heartbeat-{run_id}"
        )
        adapter: Any | None = None

        try:
            # Snapshot materialization can scan thousands of rows.  Keep it off the
            # event loop so the already-claimed lease can heartbeat while the
            # database driver and Python object construction are busy.
            model, questions, generation, prompt_template, execution = await asyncio.to_thread(
                self._load_snapshots, run_id
            )
            if lease_lost.is_set():
                raise _LeaseUnavailable("heartbeat_fence_lost")
            if shutdown_requested is not None and shutdown_requested.is_set():
                raise _ShutdownRequested("process_shutdown_requested")
            timeouts = dict(execution.get("timeouts_seconds", {}))
            retry_policy = dict(execution.get("retry_policy", {}))
            adapter = build_adapter(
                model.provider_type,
                base_url=model.base_url,
                remote_model_name=model.remote_model_name,
                api_key_env=model.api_key_env,
                api_key=model.api_key,
                connect_timeout_seconds=timeouts.get("connect"),
                read_timeout_seconds=timeouts.get("read"),
                write_timeout_seconds=timeouts.get("write"),
                pool_timeout_seconds=timeouts.get("pool"),
                max_retries=retry_policy.get("max_retries"),
                retry_backoff_base_seconds=retry_policy.get("backoff_base_seconds"),
                retry_backoff_cap_seconds=retry_policy.get("backoff_cap_seconds"),
            )
            configured_concurrency = execution.get("concurrency", 1)
            concurrency = max(1, min(4, int(configured_concurrency)))

            async def evaluate_bounded(question: _QuestionSnapshot) -> bool:
                if lease_lost.is_set():
                    raise _LeaseUnavailable("heartbeat_fence_lost")
                if shutdown_requested is not None and shutdown_requested.is_set():
                    return False
                if self._cancellation_requested(run_id):
                    return False
                await self._evaluate_question(
                    lease,
                    model,
                    question,
                    generation,
                    prompt_template,
                    adapter,
                )
                return True

            await self._run_questions(
                questions,
                evaluate=evaluate_bounded,
                concurrency=concurrency,
                lease_lost=lease_lost,
            )
            if lease_lost.is_set():
                raise _LeaseUnavailable("heartbeat_fence_lost")
            if shutdown_requested is not None and shutdown_requested.is_set():
                raise _ShutdownRequested("process_shutdown_requested")
            final_status = (
                RunStatus.CANCELLED if self._cancellation_requested(run_id) else RunStatus.COMPLETED
            )
            finished_status = self._finish(lease, final_status)
            logger.info(
                "Evaluation run finish transition resolved",
                extra={
                    "event": "run_attempt_finished",
                    "run_id": run_id,
                    "worker_id": lease.owner,
                    "attempt": lease.attempt,
                    "lease_token": lease.token,
                    "result": finished_status.value if finished_status else "fence_lost",
                },
            )
            return True
        except asyncio.CancelledError:
            logger.warning(
                "Evaluation run interrupted; durable lease expiry will recover it",
                extra={
                    "event": "run_shutdown_lease_expiry",
                    "run_id": run_id,
                    "worker_id": lease.owner,
                    "attempt": lease.attempt,
                    "lease_token": lease.token,
                    "result": "interrupted",
                },
            )
            raise
        except _ShutdownRequested:
            logger.info(
                "Evaluation run drained for shutdown; durable lease expiry will recover it",
                extra={
                    "event": "run_shutdown_lease_expiry",
                    "run_id": run_id,
                    "worker_id": lease.owner,
                    "attempt": lease.attempt,
                    "lease_token": lease.token,
                    "result": "drained",
                },
            )
            return False
        except _LeaseUnavailable:
            if self._lease_repository.finish_cancelled(lease):
                logger.info(
                    "Evaluation run stopped after cancellation",
                    extra={
                        "event": "run_cancelled",
                        "run_id": run_id,
                        "worker_id": lease.owner,
                        "attempt": lease.attempt,
                        "lease_token": lease.token,
                        "result": "cancelled",
                    },
                )
            else:
                logger.warning(
                    "Evaluation run stopped after losing its lease",
                    extra={
                        "event": "run_lease_lost",
                        "run_id": run_id,
                        "worker_id": lease.owner,
                        "attempt": lease.attempt,
                        "lease_token": lease.token,
                        "result": "fenced",
                    },
                )
            return True
        except Exception as exc:  # Run-level isolation; details are deliberately sanitized.
            logger.error(
                "Evaluation run failed",
                extra={
                    "event": "run_attempt_failed",
                    "run_id": run_id,
                    "worker_id": lease.owner,
                    "attempt": lease.attempt,
                    "lease_token": lease.token,
                    "error_code": f"runner_error:{type(exc).__name__}",
                    "result": "retry_or_dead_letter",
                },
            )
            disposition = self._lease_repository.fail_attempt(
                lease, error_code=f"runner_error:{type(exc).__name__}"
            )
            logger.warning(
                "Evaluation run failure transition resolved",
                extra={
                    "event": "run_attempt_failure_resolved",
                    "run_id": run_id,
                    "worker_id": lease.owner,
                    "attempt": lease.attempt,
                    "lease_token": lease.token,
                    "error_code": f"runner_error:{type(exc).__name__}",
                    "result": disposition.value,
                },
            )
            return True
        finally:
            heartbeat_stop.set()
            try:
                await heartbeat_task
            finally:
                close_adapter = getattr(adapter, "aclose", None)
                if close_adapter is not None:
                    try:
                        await close_adapter()
                    except Exception as exc:
                        logger.warning(
                            "Evaluation adapter cleanup failed",
                            extra={
                                "event": "run_adapter_cleanup_failed",
                                "run_id": run_id,
                                "worker_id": lease.owner,
                                "attempt": lease.attempt,
                                "lease_token": lease.token,
                                "error_code": f"adapter_cleanup_error:{type(exc).__name__}",
                                "result": "ignored_after_cleanup_attempt",
                            },
                        )

    def _claim(self, run_id: str) -> RunLease | None:
        return self._lease_repository.claim(run_id, owner=self._worker_id)

    async def _heartbeat(
        self,
        lease: RunLease,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_seconds)
            except TimeoutError:
                try:
                    renewed = self._lease_repository.heartbeat(lease)
                except Exception:
                    logger.error(
                        "Run lease heartbeat failed",
                        extra={
                            "event": "run_heartbeat_failed",
                            "run_id": lease.run_id,
                            "worker_id": lease.owner,
                            "attempt": lease.attempt,
                            "lease_token": lease.token,
                            "result": "lease_lost",
                        },
                    )
                    lease_lost.set()
                    return
                if renewed is None:
                    logger.warning(
                        "Run lease heartbeat rejected",
                        extra={
                            "event": "run_heartbeat_rejected",
                            "run_id": lease.run_id,
                            "worker_id": lease.owner,
                            "attempt": lease.attempt,
                            "lease_token": lease.token,
                            "result": "fenced",
                        },
                    )
                    lease_lost.set()
                    return
                logger.debug(
                    "Run lease heartbeat renewed",
                    extra={
                        "event": "run_heartbeat_renewed",
                        "run_id": lease.run_id,
                        "worker_id": lease.owner,
                        "attempt": lease.attempt,
                        "lease_token": lease.token,
                        "result": "renewed",
                    },
                )

    @staticmethod
    async def _run_questions(
        questions: list[_QuestionSnapshot],
        *,
        evaluate: Callable[[_QuestionSnapshot], Awaitable[bool]],
        concurrency: int,
        lease_lost: asyncio.Event,
    ) -> None:
        """Run a large question set with a fixed number of consumer tasks.

        A shared iterator keeps scheduling memory constant instead of creating one
        coroutine and task per question.  Returning ``False`` from ``evaluate``
        stops consumers from taking more work after shutdown or cancellation.
        Lease loss and unexpected errors still cancel every in-flight consumer.
        """

        if not questions:
            return

        question_iterator = iter(questions)
        stop_scheduling = asyncio.Event()

        async def consume_questions() -> None:
            while not stop_scheduling.is_set():
                if lease_lost.is_set():
                    raise _LeaseUnavailable("heartbeat_fence_lost")
                try:
                    question = next(question_iterator)
                except StopIteration:
                    return
                if not await evaluate(question):
                    stop_scheduling.set()
                    return

        consumer_count = min(max(1, concurrency), len(questions))
        question_tasks = [
            asyncio.create_task(
                consume_questions(),
                name=f"question-consumer-{index + 1}",
            )
            for index in range(consumer_count)
        ]

        async def gather_questions() -> None:
            await asyncio.gather(*question_tasks)

        questions_task = asyncio.create_task(
            gather_questions(),
            name="question-consumers",
        )
        lease_lost_task = asyncio.create_task(
            lease_lost.wait(),
            name="question-lease-loss-watch",
        )
        try:
            await asyncio.wait(
                {questions_task, lease_lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_lost.is_set():
                questions_task.cancel()
                await asyncio.gather(questions_task, return_exceptions=True)
                raise _LeaseUnavailable("heartbeat_fence_lost")
            await questions_task
        finally:
            for task in question_tasks:
                task.cancel()
            questions_task.cancel()
            lease_lost_task.cancel()
            await asyncio.gather(*question_tasks, return_exceptions=True)
            await asyncio.gather(
                questions_task,
                lease_lost_task,
                return_exceptions=True,
            )

    def _load_snapshots(
        self, run_id: str
    ) -> tuple[
        _ModelSnapshot,
        list[_QuestionSnapshot],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]:
        with self._session_factory() as session:
            run = session.get(EvaluationRun, run_id)
            if run is None:
                raise LookupError("run_not_found")
            benchmark = session.get(Benchmark, run.benchmark_id)
            if benchmark is None:
                raise LookupError("run_dependency_missing")
            persisted_snapshot = dict(run.model_parameters_snapshot or {})
            model_values = dict(persisted_snapshot.get("model", {}))
            required_model_fields = {
                "adapter_type",
                "input_price_per_million",
                "output_price_per_million",
            }
            if not required_model_fields.issubset(model_values):
                raise ValueError("run_model_snapshot_incomplete")
            rows = session.scalars(
                select(Question)
                .where(
                    Question.benchmark_id == benchmark.id,
                    ~select(EvaluationResponse.id)
                    .where(
                        EvaluationResponse.run_id == run_id,
                        EvaluationResponse.question_id == Question.id,
                    )
                    .exists(),
                )
                .order_by(Question.position)
            ).all()
            model_snapshot = _ModelSnapshot(
                provider_type=str(model_values["adapter_type"]),
                base_url=(
                    str(model_values["base_url"])
                    if model_values.get("base_url") is not None
                    else None
                ),
                remote_model_name=(
                    str(model_values["remote_model_name"])
                    if model_values.get("remote_model_name") is not None
                    else None
                ),
                credential_source=str(
                    model_values.get("credential_source")
                    or ("environment" if model_values.get("api_key_env") is not None else "none")
                ),
                api_key_env=(
                    str(model_values["api_key_env"])
                    if model_values.get("api_key_env") is not None
                    else None
                ),
                api_key=None,
                input_price=(
                    Decimal(str(model_values["input_price_per_million"]))
                    if model_values["input_price_per_million"] is not None
                    else None
                ),
                output_price=(
                    Decimal(str(model_values["output_price_per_million"]))
                    if model_values["output_price_per_million"] is not None
                    else None
                ),
            )
            credential_source = model_snapshot.credential_source
            if credential_source not in {"none", "environment", "stored"}:
                raise ValueError("run_credential_snapshot_invalid")
            if model_snapshot.provider_type == "mock":
                if (
                    credential_source != "none"
                    or model_snapshot.base_url is not None
                    or model_snapshot.remote_model_name is not None
                    or model_snapshot.api_key_env is not None
                ):
                    raise ValueError("run_credential_snapshot_invalid")
            elif model_snapshot.provider_type == "openai_compatible":
                if model_snapshot.base_url is None or model_snapshot.remote_model_name is None:
                    raise ValueError("run_model_snapshot_incomplete")
                if credential_source == "environment":
                    if not model_snapshot.api_key_env:
                        raise ValueError("run_credential_snapshot_invalid")
                elif credential_source == "stored":
                    if model_snapshot.api_key_env is not None:
                        raise ValueError("run_credential_snapshot_invalid")
                else:
                    raise ValueError("run_credential_snapshot_invalid")
            else:
                raise ValueError("run_model_snapshot_invalid")

            if credential_source == "stored":
                snapshot_model_id = model_values.get("id")
                if (
                    snapshot_model_id != run.model_id
                    or not run.model_id
                    or model_snapshot.base_url is None
                ):
                    raise ValueError("run_model_snapshot_incomplete")
                credential = session.get(ModelCredential, run.model_id)
                if credential is None:
                    raise ValueError("run_stored_credential_missing")
                encrypted = EncryptedCredential(
                    key_id=credential.key_id,
                    algorithm=credential.algorithm,
                    nonce=credential.nonce,
                    ciphertext=credential.ciphertext,
                )
                api_key = CredentialKeyring.from_file(self._settings.credential_keys_file).decrypt(
                    encrypted,
                    model_id=run.model_id,
                    # Security invariant: bind to the immutable Run target, never
                    # the Model's current Base URL.
                    provider_base_url=model_snapshot.base_url,
                )
                model_snapshot = _ModelSnapshot(
                    provider_type=model_snapshot.provider_type,
                    base_url=model_snapshot.base_url,
                    remote_model_name=model_snapshot.remote_model_name,
                    credential_source=model_snapshot.credential_source,
                    api_key_env=None,
                    api_key=api_key,
                    input_price=model_snapshot.input_price,
                    output_price=model_snapshot.output_price,
                )
            question_snapshots = [
                _QuestionSnapshot(
                    id=row.id,
                    external_id=row.external_id,
                    question_type=row.question_type.value,
                    prompt=row.prompt,
                    choices=dict(row.choices) if row.choices else None,
                    reference_answer=row.reference_answer,
                    evaluator_config=dict(row.evaluator_config or {}),
                    metadata=dict(row.metadata_ or {}),
                )
                for row in rows
            ]
            generation = dict(persisted_snapshot.get("generation", {}))
            return (
                model_snapshot,
                question_snapshots,
                generation,
                dict(run.prompt_template_snapshot),
                dict(persisted_snapshot.get("execution", {})),
            )

    async def _evaluate_question(
        self,
        lease: RunLease,
        model: _ModelSnapshot,
        question: _QuestionSnapshot,
        generation: dict[str, Any],
        prompt_template: dict[str, Any],
        adapter: Any,
    ) -> None:
        run_id = lease.run_id
        evaluator = get_evaluator(question.question_type)
        config = dict(generation)
        if model.provider_type == "mock":
            config["mock_response"] = question.metadata.get("mock_response", "")
            config["mock_generation_delay_seconds"] = self._mock_generation_delay_seconds
            if "mock_error" in question.metadata:
                config["mock_error"] = question.metadata["mock_error"]
            config.setdefault("mock_input_tokens", question.metadata.get("mock_input_tokens", 8))
            config.setdefault("mock_output_tokens", question.metadata.get("mock_output_tokens", 2))
            config.setdefault("mock_latency_ms", question.metadata.get("mock_latency_ms", 1.0))

        messages = self._render_messages(question, prompt_template)
        try:
            generated = await adapter.generate(messages, config)
            if not generated.text.strip():
                raise AdapterError("empty_response", "The model returned an empty response.")
        except AdapterError as exc:
            response = EvaluationResponse(
                run_id=run_id,
                question_id=question.id,
                raw_response=None,
                parsed_answer=None,
                reference_answer_snapshot=question.reference_answer,
                score=0.0,
                evaluator_name=getattr(evaluator, "evaluator_name", question.question_type),
                error_type=exc.error_type,
                error_message=exc.error_message,
            )
        except Exception as exc:
            logger.error(
                "Question processing failed",
                extra={
                    "event": "question_processing_failed",
                    "run_id": run_id,
                    "question_id": question.id,
                    "worker_id": lease.owner,
                    "attempt": lease.attempt,
                    "lease_token": lease.token,
                    "error_code": f"question_internal_error:{type(exc).__name__}",
                    "result": "error_response",
                },
            )
            response = EvaluationResponse(
                run_id=run_id,
                question_id=question.id,
                raw_response=None,
                parsed_answer=None,
                reference_answer_snapshot=question.reference_answer,
                score=0.0,
                evaluator_name=getattr(evaluator, "evaluator_name", question.question_type),
                error_type="question_internal_error",
                error_message=f"Question processing failed: {type(exc).__name__}",
            )
        else:
            evaluator_config = dict(question.evaluator_config)
            if question.choices:
                evaluator_config["choices"] = question.choices
            try:
                evaluated = evaluator.evaluate(
                    generated.text,
                    question.reference_answer,
                    evaluator_config,
                )
            except Exception as exc:
                logger.error(
                    "Evaluator failed for question",
                    extra={
                        "event": "question_evaluator_failed",
                        "run_id": run_id,
                        "question_id": question.id,
                        "worker_id": lease.owner,
                        "attempt": lease.attempt,
                        "lease_token": lease.token,
                        "error_code": f"evaluator_internal_error:{type(exc).__name__}",
                        "result": "error_response",
                    },
                )
                response = EvaluationResponse(
                    run_id=run_id,
                    question_id=question.id,
                    raw_response=generated.text,
                    parsed_answer=None,
                    reference_answer_snapshot=question.reference_answer,
                    score=0.0,
                    evaluator_name=getattr(evaluator, "evaluator_name", question.question_type),
                    latency_ms=float(generated.latency_ms),
                    input_tokens=generated.input_tokens,
                    output_tokens=generated.output_tokens,
                    estimated_cost=self._cost(
                        model, generated.input_tokens, generated.output_tokens
                    ),
                    error_type="evaluator_internal_error",
                    error_message=f"Evaluator failed: {type(exc).__name__}",
                )
            else:
                parse_error = evaluated.parse_error
                error_type, error_message = self._parse_error_evidence(
                    parse_error,
                    dict(generated.metadata),
                )
                response = EvaluationResponse(
                    run_id=run_id,
                    question_id=question.id,
                    raw_response=generated.text,
                    parsed_answer=evaluated.parsed_answer,
                    reference_answer_snapshot=question.reference_answer,
                    score=float(evaluated.score),
                    evaluator_name=evaluated.evaluator_name,
                    latency_ms=float(generated.latency_ms),
                    input_tokens=generated.input_tokens,
                    output_tokens=generated.output_tokens,
                    estimated_cost=self._cost(
                        model, generated.input_tokens, generated.output_tokens
                    ),
                    error_type=error_type,
                    error_message=error_message,
                )

        disposition = self._lease_repository.persist_response(lease, response)
        logger.info(
            "Question evidence persistence resolved",
            extra={
                "event": "question_evidence_persisted",
                "run_id": run_id,
                "question_id": question.id,
                "worker_id": lease.owner,
                "attempt": lease.attempt,
                "lease_token": lease.token,
                "result": disposition.value,
                "error_code": response.error_type or "none",
            },
        )
        if disposition == ResponseDisposition.FENCE_LOST:
            raise _LeaseUnavailable("response_fence_lost")

    @staticmethod
    def _render_messages(
        question: _QuestionSnapshot, prompt_template: dict[str, Any]
    ) -> list[dict[str, str]]:
        choices = ""
        if question.choices:
            choices = "\n".join(
                f"{key}. {value}" for key, value in sorted(question.choices.items())
            )
        system = str(prompt_template.get("system", "You are an objective benchmark participant."))
        user_template = str(prompt_template.get("user", "{prompt}\n{choices}"))
        user = user_template.replace("{prompt}", question.prompt).replace("{choices}", choices)
        messages = [{"role": "user", "content": user.strip()}]
        if system.strip():
            messages.insert(0, {"role": "system", "content": system})
        return messages

    @staticmethod
    def _cost(
        model: _ModelSnapshot, input_tokens: int | None, output_tokens: int | None
    ) -> Decimal | None:
        if (
            input_tokens is None
            or output_tokens is None
            or model.input_price is None
            or model.output_price is None
        ):
            return None
        input_count = Decimal(input_tokens)
        output_count = Decimal(output_tokens)
        return ((input_count * model.input_price) + (output_count * model.output_price)) / Decimal(
            1_000_000
        )

    @staticmethod
    def _parse_error_evidence(
        parse_error: str | None,
        generation_metadata: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        """Turn a parser failure plus Provider finish reason into actionable evidence."""

        if parse_error is None:
            return None, None
        if generation_metadata.get("finish_reason") == "length":
            return (
                "output_truncated",
                "Provider stopped at the output token limit before a valid final "
                f"answer was parsed ({parse_error}).",
            )
        return "parse_error", parse_error

    def _cancellation_requested(self, run_id: str) -> bool:
        with self._session_factory() as session:
            return bool(
                session.scalar(
                    select(EvaluationRun.cancellation_requested).where(EvaluationRun.id == run_id)
                )
            )

    def _finish(self, lease: RunLease, status: RunStatus) -> RunStatus | None:
        with self._session_factory() as session, session.begin():
            run = self._lease_repository.lock_owned_run(session, lease, allow_cancel_requested=True)
            if run is None:
                return None
            planned = run.total_questions
            completed_response_count = aggregate_run_evidence(session, run)
            if (
                status == RunStatus.COMPLETED
                and not run.cancellation_requested
                and completed_response_count != planned
            ):
                raise RuntimeError("run_response_set_incomplete")
            actual_status = RunStatus.CANCELLED if run.cancellation_requested else status
            run.status = actual_status
            run.finished_at = utc_now()
            run.next_attempt_at = None
            run.lease_owner = None
            run.lease_expires_at = None
            run.heartbeat_at = None
            return actual_status
