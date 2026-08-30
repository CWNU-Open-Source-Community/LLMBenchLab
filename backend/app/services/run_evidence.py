"""Pure protocol-v1 metric derivation shared by writers and read projections."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CanonicalRunEvidence:
    """Derived Run metrics, including exact all-or-nothing usage fields."""

    completed_questions: int
    correct_questions: int
    error_questions: int
    score: float
    completion_rate: float
    answered_accuracy: float | None
    average_latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: Decimal | None


def canonical_run_evidence(
    *,
    planned_questions: int,
    response_count: int,
    score_sum: float,
    completed_outputs: int,
    evaluable_responses: int,
    error_responses: int,
    average_latency_ms: float | None,
    known_input_tokens: int,
    input_token_reports: int,
    known_output_tokens: int,
    output_token_reports: int,
    known_estimated_cost: Decimal,
    estimated_cost_reports: int,
) -> CanonicalRunEvidence:
    """Apply the protocol-v1 denominators and exact usage coverage rule."""

    correct_questions = round(float(score_sum))
    return CanonicalRunEvidence(
        completed_questions=response_count,
        correct_questions=correct_questions,
        error_questions=error_responses,
        score=(float(score_sum) / planned_questions * 100) if planned_questions else 0.0,
        completion_rate=(completed_outputs / planned_questions * 100) if planned_questions else 0.0,
        answered_accuracy=(correct_questions / evaluable_responses * 100)
        if evaluable_responses
        else None,
        average_latency_ms=average_latency_ms,
        input_tokens=(
            known_input_tokens if response_count and input_token_reports == response_count else None
        ),
        output_tokens=(
            known_output_tokens
            if response_count and output_token_reports == response_count
            else None
        ),
        estimated_cost=(
            known_estimated_cost
            if response_count and estimated_cost_reports == response_count
            else None
        ),
    )
