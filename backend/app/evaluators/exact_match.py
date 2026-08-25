"""Strict exact-match evaluation with declared normalization only."""

from __future__ import annotations

import re

from .base import EvaluationResult, Evaluator, EvaluatorConfig

_WHITESPACE_RE = re.compile(r"\s+", flags=re.UNICODE)


def _normalize_text(value: object, *, normalize_whitespace: bool, case_sensitive: bool) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if normalize_whitespace:
        text = _WHITESPACE_RE.sub(" ", text)
    if not case_sensitive:
        text = text.casefold()
    return text


class ExactMatchEvaluator(Evaluator):
    """Compare normalized strings without fuzzy or semantic matching."""

    evaluator_name = "exact_match_v1"

    def evaluate(
        self,
        raw_response: object,
        reference_answer: object,
        config: EvaluatorConfig | None = None,
    ) -> EvaluationResult:
        settings = config or {}
        case_sensitive = bool(settings.get("case_sensitive", False))
        normalize_whitespace = bool(settings.get("normalize_whitespace", True))
        metadata = {
            "case_sensitive": case_sensitive,
            "normalize_whitespace": normalize_whitespace,
        }
        if raw_response is None or not str(raw_response).strip():
            return EvaluationResult(
                parsed_answer=None,
                score=0.0,
                correct=False,
                evaluator_name=self.evaluator_name,
                metadata=metadata,
                parse_error="empty_response",
            )
        if reference_answer is None:
            return EvaluationResult(
                parsed_answer=None,
                score=0.0,
                correct=False,
                evaluator_name=self.evaluator_name,
                metadata=metadata,
                parse_error="invalid_reference_answer",
            )

        parsed = _normalize_text(
            raw_response,
            normalize_whitespace=normalize_whitespace,
            case_sensitive=case_sensitive,
        )
        reference = _normalize_text(
            reference_answer,
            normalize_whitespace=normalize_whitespace,
            case_sensitive=case_sensitive,
        )
        correct = parsed == reference
        return EvaluationResult(
            parsed_answer=parsed,
            score=1.0 if correct else 0.0,
            correct=correct,
            evaluator_name=self.evaluator_name,
            metadata=metadata,
        )
