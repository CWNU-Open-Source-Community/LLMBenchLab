"""Durable run state around a deliberately small in-process task manager."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.adapters import AdapterError, build_adapter
from app.core.time import utc_now
from app.evaluators import get_evaluator
from app.models import (
    Benchmark,
    EvaluationResponse,
    EvaluationRun,
    Question,
    RunStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ModelSnapshot:
    provider_type: str
    base_url: str | None
    remote_model_name: str | None
    api_key_env: str | None
    input_price: Decimal | None
    output_price: Decimal | None


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


class EvaluationRunner:
    """Execute one claimed Run while isolating every question failure."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def execute(self, run_id: str) -> None:
        """Atomically claim and execute ``run_id`` exactly once."""

        if not self._claim(run_id):
            return

        try:
            model, questions, generation, prompt_template, execution = self._load_snapshots(run_id)
            timeouts = dict(execution.get("timeouts_seconds", {}))
            retry_policy = dict(execution.get("retry_policy", {}))
            adapter = build_adapter(
                model.provider_type,
                base_url=model.base_url,
                remote_model_name=model.remote_model_name,
                api_key_env=model.api_key_env,
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
            semaphore = asyncio.Semaphore(concurrency)

            async def evaluate_bounded(question: _QuestionSnapshot) -> None:
                async with semaphore:
                    if self._cancellation_requested(run_id):
                        return
                    await self._evaluate_question(
                        run_id,
                        model,
                        question,
                        generation,
                        prompt_template,
                        adapter,
                    )

            await asyncio.gather(*(evaluate_bounded(question) for question in questions))
            final_status = (
                RunStatus.CANCELLED if self._cancellation_requested(run_id) else RunStatus.COMPLETED
            )
            self._finish(run_id, final_status)
        except asyncio.CancelledError:
            self._fail_run(run_id, "interrupted_by_process_shutdown")
            raise
        except Exception as exc:  # Run-level isolation; details are deliberately sanitized.
            logger.exception("Evaluation run %s failed", run_id)
            self._fail_run(run_id, f"runner_error: {type(exc).__name__}")

    def _claim(self, run_id: str) -> bool:
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(EvaluationRun)
                .where(
                    EvaluationRun.id == run_id,
                    EvaluationRun.status == RunStatus.PENDING,
                    EvaluationRun.cancellation_requested.is_(False),
                )
                .values(status=RunStatus.RUNNING, started_at=utc_now(), error_message=None)
            )
            return bool(result.rowcount == 1)

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
                .where(Question.benchmark_id == benchmark.id)
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
                api_key_env=(
                    str(model_values["api_key_env"])
                    if model_values.get("api_key_env") is not None
                    else None
                ),
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
        run_id: str,
        model: _ModelSnapshot,
        question: _QuestionSnapshot,
        generation: dict[str, Any],
        prompt_template: dict[str, Any],
        adapter: Any,
    ) -> None:
        evaluator = get_evaluator(question.question_type)
        config = dict(generation)
        if model.provider_type == "mock":
            config["mock_response"] = question.metadata.get("mock_response", "")
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
            logger.exception("Question %s failed in run %s", question.external_id, run_id)
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
                    "Evaluator %s failed for question %s in run %s",
                    type(exc).__name__,
                    question.external_id,
                    run_id,
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
                    error_type="parse_error" if parse_error else None,
                    error_message=parse_error,
                )

        with self._session_factory() as session, session.begin():
            session.add(response)
            run = session.get(EvaluationRun, run_id)
            if run is None:
                raise LookupError("run_disappeared")
            run.completed_questions += 1

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
        return [{"role": "system", "content": system}, {"role": "user", "content": user.strip()}]

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

    def _cancellation_requested(self, run_id: str) -> bool:
        with self._session_factory() as session:
            return bool(
                session.scalar(
                    select(EvaluationRun.cancellation_requested).where(EvaluationRun.id == run_id)
                )
            )

    def _finish(self, run_id: str, status: RunStatus) -> None:
        with self._session_factory() as session, session.begin():
            run = session.get(EvaluationRun, run_id)
            if run is None:
                return
            aggregate = session.execute(
                select(
                    func.count(EvaluationResponse.id),
                    func.coalesce(func.sum(EvaluationResponse.score), 0.0),
                    func.count(EvaluationResponse.id).filter(
                        EvaluationResponse.raw_response.is_not(None),
                        EvaluationResponse.raw_response != "",
                    ),
                    func.count(EvaluationResponse.id).filter(
                        EvaluationResponse.error_type.is_(None),
                        EvaluationResponse.raw_response.is_not(None),
                        EvaluationResponse.raw_response != "",
                    ),
                    func.count(EvaluationResponse.id).filter(
                        EvaluationResponse.error_type.is_not(None)
                    ),
                    func.avg(EvaluationResponse.latency_ms),
                    func.coalesce(func.sum(EvaluationResponse.input_tokens), 0),
                    func.count(EvaluationResponse.input_tokens),
                    func.coalesce(func.sum(EvaluationResponse.output_tokens), 0),
                    func.count(EvaluationResponse.output_tokens),
                    func.coalesce(func.sum(EvaluationResponse.estimated_cost), 0),
                    func.count(EvaluationResponse.estimated_cost),
                ).where(EvaluationResponse.run_id == run_id)
            ).one()
            (
                response_count,
                score_sum,
                completed_outputs,
                evaluable,
                errors,
                avg_latency,
                in_tok,
                in_reports,
                out_tok,
                out_reports,
                cost,
                cost_reports,
            ) = aggregate
            planned = run.total_questions
            correct = round(float(score_sum or 0))
            run.correct_questions = correct
            run.error_questions = int(errors or 0)
            run.score = (float(score_sum or 0) / planned * 100) if planned else 0.0
            run.completion_rate = (int(completed_outputs or 0) / planned * 100) if planned else 0.0
            run.answered_accuracy = (
                (correct / int(evaluable) * 100) if int(evaluable or 0) else None
            )
            run.average_latency_ms = float(avg_latency) if avg_latency is not None else None
            completed_response_count = int(response_count or 0)
            run.input_tokens = (
                int(in_tok or 0)
                if completed_response_count and int(in_reports or 0) == completed_response_count
                else None
            )
            run.output_tokens = (
                int(out_tok or 0)
                if completed_response_count and int(out_reports or 0) == completed_response_count
                else None
            )
            run.estimated_cost = (
                Decimal(cost or 0)
                if completed_response_count and int(cost_reports or 0) == completed_response_count
                else None
            )
            run.status = status
            run.finished_at = utc_now()

    def _fail_run(self, run_id: str, message: str) -> None:
        with self._session_factory() as session, session.begin():
            run = session.get(EvaluationRun, run_id)
            if run is None or run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
                return
            run.status = RunStatus.FAILED
            run.error_message = message[:1000]
            run.finished_at = utc_now()


class EvaluationTaskManager:
    """Deduplicate in-process tasks and expose cooperative scheduling/shutdown."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._runner = EvaluationRunner(session_factory)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def schedule(self, run_id: str) -> bool:
        """Schedule a run once in the current process and return whether accepted."""

        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(self._runner.execute(run_id), name=f"evaluation-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda completed, rid=run_id: self._discard(rid, completed))
        return True

    def _discard(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)
        if not task.cancelled():
            with suppress(asyncio.CancelledError):
                task.exception()

    async def shutdown(self) -> None:
        """Cancel and await tasks during application shutdown."""

        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    @property
    def active_run_ids(self) -> frozenset[str]:
        return frozenset(run_id for run_id, task in self._tasks.items() if not task.done())
