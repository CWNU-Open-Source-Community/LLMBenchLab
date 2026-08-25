"""Conservative extraction for letter-keyed multiple-choice answers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .base import EvaluationResult, Evaluator, EvaluatorConfig

_KEY = r"([A-Z])(?![A-Z])"
_EXPLICIT_PATTERNS = (
    re.compile(
        rf"最终\s*答案\s*(?:是|为|[:\uff1a])\s*[\(\uff08]?\s*{_KEY}\s*[\)\uff09]?",
        re.I,
    ),
    re.compile(
        rf"(?<!最终)答案\s*(?:是|为|[:\uff1a])\s*[\(\uff08]?\s*{_KEY}\s*[\)\uff09]?",
        re.I,
    ),
    re.compile(
        rf"选择\s*(?:是|为|[:\uff1a])?\s*[\(\uff08]?\s*{_KEY}\s*[\)\uff09]?",
        re.I,
    ),
    re.compile(
        rf"\b(?:the\s+)?(?:final\s+)?answer\s*(?:is|:|\uff1a)"
        rf"\s*[\(\uff08]?\s*{_KEY}\s*[\)\uff09]?",
        re.I,
    ),
)
_BARE_ANSWER_RE = re.compile(r"^\s*(?:([A-Z])|([A-Z])\.|\(([A-Z])\)|\uff08([A-Z])\uff09)\s*$", re.I)
_EXPLICIT_ALTERNATIVE_RE = re.compile(
    r"^\s*(?:or|或|或者|/)\s*[\(\uff08]?\s*([A-Z])(?![A-Z])", re.I
)


def _choice_keys(config: EvaluatorConfig) -> set[str]:
    configured = config.get("choices", config.get("valid_choices"))
    if configured is None:
        return {"A", "B", "C", "D"}
    if isinstance(configured, Mapping):
        values = configured.keys()
    elif isinstance(configured, Sequence) and not isinstance(configured, (str, bytes, bytearray)):
        values = configured
    else:
        return set()
    keys = {str(value).strip().upper() for value in values}
    if not keys or any(len(key) != 1 or not ("A" <= key <= "Z") for key in keys):
        return set()
    return keys


def _bare_candidate(text: str) -> str | None:
    match = _BARE_ANSWER_RE.fullmatch(text)
    if not match:
        return None
    return next(group.upper() for group in match.groups() if group is not None)


class MultipleChoiceEvaluator(Evaluator):
    """Parse explicit answer markers before considering isolated answer lines."""

    evaluator_name = "multiple_choice_v1"

    def evaluate(
        self,
        raw_response: object,
        reference_answer: object,
        config: EvaluatorConfig | None = None,
    ) -> EvaluationResult:
        settings = config or {}
        valid_choices = _choice_keys(settings)
        metadata: dict[str, object] = {"valid_choices": sorted(valid_choices)}
        if not valid_choices:
            return self._error("invalid_choices_config", metadata=metadata)
        if raw_response is None or not str(raw_response).strip():
            return self._error("empty_response", metadata=metadata)

        reference = "" if reference_answer is None else str(reference_answer).strip().upper()
        if reference not in valid_choices:
            return self._error("invalid_reference_answer", metadata=metadata)

        text = str(raw_response).replace("\r\n", "\n").replace("\r", "\n").strip()
        explicit: list[str] = []
        for pattern in _EXPLICIT_PATTERNS:
            for match in pattern.finditer(text):
                explicit.append(match.group(1).upper())
                alternative = _EXPLICIT_ALTERNATIVE_RE.match(text[match.end() :])
                if alternative:
                    explicit.append(alternative.group(1).upper())
        if explicit:
            metadata["parser"] = "explicit_answer"
            return self._score_candidates(explicit, reference, valid_choices, metadata)

        bare = _bare_candidate(text)
        if bare is not None:
            metadata["parser"] = "bare_answer"
            return self._score_candidates([bare], reference, valid_choices, metadata)

        answer_lines = [
            candidate
            for line in text.split("\n")
            if (candidate := _bare_candidate(line.strip())) is not None
        ]
        if answer_lines:
            metadata["parser"] = "isolated_answer_line"
            return self._score_candidates(answer_lines, reference, valid_choices, metadata)
        return self._error("choice_not_found", metadata=metadata)

    def _score_candidates(
        self,
        candidates: list[str],
        reference: str,
        valid_choices: set[str],
        metadata: dict[str, object],
    ) -> EvaluationResult:
        unique = set(candidates)
        metadata["candidates"] = sorted(unique)
        if len(unique) != 1:
            return self._error("ambiguous_choice", metadata=metadata)
        parsed = next(iter(unique))
        if parsed not in valid_choices:
            return self._error("invalid_choice", parsed_answer=parsed, metadata=metadata)
        correct = parsed == reference
        return EvaluationResult(
            parsed_answer=parsed,
            score=1.0 if correct else 0.0,
            correct=correct,
            evaluator_name=self.evaluator_name,
            metadata=metadata,
        )

    def _error(
        self,
        parse_error: str,
        *,
        metadata: Mapping[str, object],
        parsed_answer: str | None = None,
    ) -> EvaluationResult:
        return EvaluationResult(
            parsed_answer=parsed_answer,
            score=0.0,
            correct=False,
            evaluator_name=self.evaluator_name,
            metadata=metadata,
            parse_error=parse_error,
        )
