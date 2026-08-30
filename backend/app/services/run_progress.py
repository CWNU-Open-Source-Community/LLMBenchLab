"""Read-only fixed-block projections for live Run Detail polling."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EvaluationResponse, EvaluationRun, Question
from app.schemas.evaluation_progress import (
    PROGRESS_BLOCK_SIZE,
    EvaluationProgressOutcome,
)
from app.services.run_evidence import CanonicalRunEvidence, canonical_run_evidence


class RunProgressIntegrityError(RuntimeError):
    """Persisted Response positions do not belong to the frozen Run plan."""


@dataclass(frozen=True, slots=True)
class RunProgressIndexProjection:
    metrics: CanonicalRunEvidence
    known_input_tokens: int
    known_output_tokens: int
    input_token_reported_responses: int
    output_token_reported_responses: int
    known_estimated_cost: Decimal
    estimated_cost_reported_responses: int
    block_response_counts: tuple[int, ...]


def progress_block_count(total_questions: int) -> int:
    """Return the number of fixed blocks needed for the frozen plan."""

    return ceil(total_questions / PROGRESS_BLOCK_SIZE) if total_questions else 0


def load_progress_index(session: Session, run: EvaluationRun) -> RunProgressIndexProjection:
    """Scan compact Response facts once and derive global and per-block aggregates."""

    has_output = EvaluationResponse.raw_response.is_not(None) & (
        EvaluationResponse.raw_response != ""
    )
    evaluable = EvaluationResponse.error_type.is_(None) & has_output
    block_index = (Question.position // PROGRESS_BLOCK_SIZE).label("block_index")
    rows = session.execute(
        select(
            Question.benchmark_id.label("benchmark_id"),
            block_index,
            func.min(Question.position).label("minimum_position"),
            func.max(Question.position).label("maximum_position"),
            func.count(EvaluationResponse.id).label("response_count"),
            func.coalesce(func.sum(EvaluationResponse.score), 0.0).label("score_sum"),
            func.count(EvaluationResponse.id).filter(has_output).label("completed_outputs"),
            func.count(EvaluationResponse.id).filter(evaluable).label("evaluable_responses"),
            func.count(EvaluationResponse.id)
            .filter(EvaluationResponse.error_type.is_not(None))
            .label("error_responses"),
            func.coalesce(func.sum(EvaluationResponse.latency_ms), 0.0).label("latency_sum"),
            func.count(EvaluationResponse.latency_ms).label("latency_reports"),
            func.coalesce(func.sum(EvaluationResponse.input_tokens), 0).label("known_input_tokens"),
            func.count(EvaluationResponse.input_tokens).label("input_token_reports"),
            func.coalesce(func.sum(EvaluationResponse.output_tokens), 0).label(
                "known_output_tokens"
            ),
            func.count(EvaluationResponse.output_tokens).label("output_token_reports"),
            func.coalesce(func.sum(EvaluationResponse.estimated_cost), 0).label(
                "known_estimated_cost"
            ),
            func.count(EvaluationResponse.estimated_cost).label("estimated_cost_reports"),
        )
        .select_from(EvaluationRun)
        .outerjoin(EvaluationResponse, EvaluationResponse.run_id == EvaluationRun.id)
        .outerjoin(Question, Question.id == EvaluationResponse.question_id)
        .where(EvaluationRun.id == run.id)
        .group_by(Question.benchmark_id, block_index)
        .order_by(Question.benchmark_id, block_index)
    ).all()

    block_counts = [0] * progress_block_count(run.total_questions)
    response_count = 0
    score_sum = 0.0
    completed_outputs = 0
    evaluable_responses = 0
    error_responses = 0
    latency_sum = 0.0
    latency_reports = 0
    known_input_tokens = 0
    input_token_reports = 0
    known_output_tokens = 0
    output_token_reports = 0
    known_estimated_cost = Decimal(0)
    estimated_cost_reports = 0

    for row in rows:
        current_count = int(row.response_count or 0)
        # The outer-join sentinel makes an empty Run return one zero-count group.
        # A positive count without a joined Question is orphaned evidence and must
        # fail closed rather than disappearing from the block index.
        if current_count == 0:
            continue
        if (
            row.benchmark_id is None
            or row.block_index is None
            or row.minimum_position is None
            or row.maximum_position is None
        ):
            raise RunProgressIntegrityError("run_progress_response_mapping_missing")
        current_block = int(row.block_index)
        block_start = current_block * PROGRESS_BLOCK_SIZE
        block_end = min(block_start + PROGRESS_BLOCK_SIZE, run.total_questions)
        if (
            row.benchmark_id != run.benchmark_id
            or not 0 <= current_block < len(block_counts)
            or int(row.minimum_position) < block_start
            or int(row.maximum_position) >= block_end
        ):
            raise RunProgressIntegrityError("run_progress_response_outside_frozen_plan")
        block_counts[current_block] += current_count
        if block_counts[current_block] > block_end - block_start:
            raise RunProgressIntegrityError("run_progress_block_response_count_invalid")
        response_count += current_count
        score_sum += float(row.score_sum or 0.0)
        completed_outputs += int(row.completed_outputs or 0)
        evaluable_responses += int(row.evaluable_responses or 0)
        error_responses += int(row.error_responses or 0)
        latency_sum += float(row.latency_sum or 0.0)
        latency_reports += int(row.latency_reports or 0)
        known_input_tokens += int(row.known_input_tokens or 0)
        input_token_reports += int(row.input_token_reports or 0)
        known_output_tokens += int(row.known_output_tokens or 0)
        output_token_reports += int(row.output_token_reports or 0)
        known_estimated_cost += Decimal(row.known_estimated_cost or 0)
        estimated_cost_reports += int(row.estimated_cost_reports or 0)

    if response_count > run.total_questions:
        raise RunProgressIntegrityError("run_progress_response_count_invalid")
    average_latency_ms = latency_sum / latency_reports if latency_reports else None
    metrics = canonical_run_evidence(
        planned_questions=run.total_questions,
        response_count=response_count,
        score_sum=score_sum,
        completed_outputs=completed_outputs,
        evaluable_responses=evaluable_responses,
        error_responses=error_responses,
        average_latency_ms=average_latency_ms,
        known_input_tokens=known_input_tokens,
        input_token_reports=input_token_reports,
        known_output_tokens=known_output_tokens,
        output_token_reports=output_token_reports,
        known_estimated_cost=known_estimated_cost,
        estimated_cost_reports=estimated_cost_reports,
    )
    return RunProgressIndexProjection(
        metrics=metrics,
        known_input_tokens=known_input_tokens,
        known_output_tokens=known_output_tokens,
        input_token_reported_responses=input_token_reports,
        output_token_reported_responses=output_token_reports,
        known_estimated_cost=known_estimated_cost,
        estimated_cost_reported_responses=estimated_cost_reports,
        block_response_counts=tuple(block_counts),
    )


def load_progress_block(
    session: Session,
    run: EvaluationRun,
    block_index: int,
) -> list[dict[str, object]]:
    """Return only allowlisted compact facts for one absolute-position block."""

    start_position = block_index * PROGRESS_BLOCK_SIZE
    end_position = min(start_position + PROGRESS_BLOCK_SIZE, run.total_questions)
    rows = session.execute(
        select(
            Question.position,
            EvaluationResponse.score,
            EvaluationResponse.latency_ms,
            EvaluationResponse.input_tokens,
            EvaluationResponse.output_tokens,
            EvaluationResponse.estimated_cost,
            EvaluationResponse.error_type,
        )
        .join(Question, Question.id == EvaluationResponse.question_id)
        .where(
            EvaluationResponse.run_id == run.id,
            Question.benchmark_id == run.benchmark_id,
            Question.position >= start_position,
            Question.position < end_position,
        )
        .order_by(Question.position)
    ).all()
    items: list[dict[str, object]] = []
    for row in rows:
        if row.error_type is not None:
            outcome = EvaluationProgressOutcome.ERROR
        elif float(row.score) == 1.0:
            outcome = EvaluationProgressOutcome.PASSED
        else:
            outcome = EvaluationProgressOutcome.WRONG
        items.append(
            {
                "position": int(row.position),
                "outcome": outcome,
                "score": float(row.score),
                "latency_ms": float(row.latency_ms) if row.latency_ms is not None else None,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "estimated_cost": (
                    float(row.estimated_cost) if row.estimated_cost is not None else None
                ),
                "error_type": row.error_type,
            }
        )
    return items
