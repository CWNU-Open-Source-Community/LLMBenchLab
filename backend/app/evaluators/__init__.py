"""Objective evaluator public API and registry."""

from __future__ import annotations

from .base import EvaluationResult, Evaluator, EvaluatorConfig
from .exact_match import ExactMatchEvaluator
from .multiple_choice import MultipleChoiceEvaluator
from .numeric import NumericEvaluator


def get_evaluator(evaluator_type: str) -> Evaluator:
    """Return a fresh evaluator for a question type or versioned evaluator name."""

    normalized = evaluator_type.strip().lower().replace("-", "_")
    if normalized.endswith("_v1"):
        normalized = normalized[:-3]
    evaluator_classes: dict[str, type[Evaluator]] = {
        "exact": ExactMatchEvaluator,
        "exact_match": ExactMatchEvaluator,
        "multiple_choice": MultipleChoiceEvaluator,
        "multiplechoice": MultipleChoiceEvaluator,
        "numeric": NumericEvaluator,
        "number": NumericEvaluator,
    }
    evaluator_class = evaluator_classes.get(normalized)
    if evaluator_class is None:
        raise ValueError(f"Unsupported evaluator type: {evaluator_type!r}")
    return evaluator_class()


build_evaluator = get_evaluator

__all__ = [
    "EvaluationResult",
    "Evaluator",
    "EvaluatorConfig",
    "ExactMatchEvaluator",
    "MultipleChoiceEvaluator",
    "NumericEvaluator",
    "build_evaluator",
    "get_evaluator",
]
