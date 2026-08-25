"""Safe Decimal-based numeric parsing and tolerance evaluation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from typing import Any

from .base import EvaluationResult, Evaluator, EvaluatorConfig

_NUMBER_CORE = r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))(?:[eE][+-]?[0-9]+)?"
_NUMBER_TOKEN_RE = re.compile(rf"(?<![\w.])({_NUMBER_CORE})(?![\w.])")
_NUMBER_FULL_RE = re.compile(rf"^\s*({_NUMBER_CORE})\s*$")
_LEADING_NUMBER_RE = re.compile(
    rf"^\s*(?:\*\*)?\s*\$?\s*[\(\uff08]?\s*({_NUMBER_CORE})(?![\w.])",
    re.I,
)
_LEADING_NON_FINITE_RE = re.compile(
    r"^\s*(?:\*\*)?\s*\$?\s*[\(\uff08]?\s*[+-]?(?:nan|inf(?:inity)?)(?!\w)",
    re.I,
)
_NON_FINITE_RE = re.compile(r"(?<!\w)[+-]?(?:nan|inf(?:inity)?)(?!\w)", re.I)
_BOXED_RE = re.compile(r"\\boxed\s*\{([^{}]*)\}")
_LEADING_BOXED_RE = re.compile(r"^\s*\\boxed\s*\{([^{}]*)\}")
_FINAL_MARKER_RE = re.compile(
    r"(?:最终\s*答案|答案)\s*(?:是|为|[:\uff1a])|"
    r"\b(?:the\s+)?(?:final\s+)?answer\s*(?:is|:|\uff1a)",
    re.I,
)
_ALTERNATIVE_AFTER_RE = re.compile(
    rf"^\s*(?:or|或|或者|/)\s*[\(\uff08]?\s*({_NUMBER_CORE})(?![\w.])", re.I
)
_EXPRESSION_RE = re.compile(rf"{_NUMBER_CORE}\s*(?:[+*/^]|-(?=\s*[0-9]))\s*{_NUMBER_CORE}")
_MAX_NUMBER_LENGTH = 256
_MAX_ADJUSTED_EXPONENT = 100_000


def _parse_decimal_token(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("not a numeric scalar")
    text = str(value).strip()
    match = _NUMBER_FULL_RE.fullmatch(text)
    if not match or len(match.group(1)) > _MAX_NUMBER_LENGTH:
        raise ValueError("invalid numeric syntax")
    try:
        number = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise ValueError("invalid numeric syntax") from exc
    if not number.is_finite():
        raise ValueError("non-finite numeric value")
    if number and abs(number.adjusted()) > _MAX_ADJUSTED_EXPONENT:
        raise ValueError("numeric exponent outside supported range")
    return number


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = str(value)
    return text.replace("e", "E")


class NumericEvaluator(Evaluator):
    """Parse one finite number and compare it using Decimal tolerances."""

    evaluator_name = "numeric_v1"

    def evaluate(
        self,
        raw_response: object,
        reference_answer: object,
        config: EvaluatorConfig | None = None,
    ) -> EvaluationResult:
        settings = config or {}
        try:
            absolute_tolerance = _parse_decimal_token(
                settings.get("absolute_tolerance", settings.get("abs_tolerance", 0))
            )
            relative_tolerance = _parse_decimal_token(
                settings.get("relative_tolerance", settings.get("rel_tolerance", 0))
            )
        except ValueError:
            return self._error("invalid_tolerance")
        if absolute_tolerance < 0 or relative_tolerance < 0:
            return self._error("invalid_tolerance")
        metadata: dict[str, Any] = {
            "absolute_tolerance": _canonical_decimal(absolute_tolerance),
            "relative_tolerance": _canonical_decimal(relative_tolerance),
        }

        try:
            reference = _parse_decimal_token(reference_answer)
        except ValueError:
            return self._error("invalid_reference_answer", metadata=metadata)
        if raw_response is None or not str(raw_response).strip():
            return self._error("empty_response", metadata=metadata)

        text = str(raw_response).replace("\r\n", "\n").replace("\r", "\n").strip()
        parsed, parser, parse_error = self._extract_number(text)
        if parse_error is not None or parsed is None:
            return self._error(parse_error or "numeric_not_found", metadata=metadata)
        metadata["parser"] = parser

        try:
            precision = (
                max(
                    50,
                    len(parsed.as_tuple().digits),
                    len(reference.as_tuple().digits),
                    len(absolute_tolerance.as_tuple().digits),
                    len(relative_tolerance.as_tuple().digits),
                )
                + 10
            )
            with localcontext() as context:
                context.prec = precision
                difference = abs(parsed - reference)
                allowed = max(absolute_tolerance, relative_tolerance * abs(reference))
                correct = difference <= allowed
        except (DecimalException, ArithmeticError):
            return self._error("numeric_comparison_error", metadata=metadata)
        metadata["difference"] = _canonical_decimal(difference)
        metadata["allowed_tolerance"] = _canonical_decimal(allowed)
        return EvaluationResult(
            parsed_answer=_canonical_decimal(parsed),
            score=1.0 if correct else 0.0,
            correct=correct,
            evaluator_name=self.evaluator_name,
            metadata=metadata,
        )

    def _extract_number(self, text: str) -> tuple[Decimal | None, str | None, str | None]:
        marker_matches = list(_FINAL_MARKER_RE.finditer(text))
        if marker_matches:
            candidates: list[Decimal] = []
            for marker in marker_matches:
                tail = text[marker.end() :]
                if _LEADING_NON_FINITE_RE.match(tail):
                    return None, None, "non_finite_number"
                boxed_match = _LEADING_BOXED_RE.match(tail)
                match = _LEADING_NUMBER_RE.match(tail) if boxed_match is None else None
                if boxed_match is None and match is None:
                    return None, None, "invalid_numeric"
                try:
                    value = _parse_decimal_token(
                        boxed_match.group(1) if boxed_match is not None else match.group(1)
                    )
                except ValueError:
                    candidate_text = (
                        boxed_match.group(1) if boxed_match is not None else match.group(1)
                    )
                    if _NON_FINITE_RE.search(candidate_text):
                        return None, None, "non_finite_number"
                    return None, None, "invalid_numeric"
                match_end = boxed_match.end() if boxed_match is not None else match.end()
                remainder = tail[match_end:]
                if re.match(r"^\s*(?:[+*/^]|-(?=\s*[0-9]))", remainder):
                    return None, None, "invalid_numeric_expression"
                alternative = _ALTERNATIVE_AFTER_RE.match(remainder)
                if alternative:
                    try:
                        candidates.append(_parse_decimal_token(alternative.group(1)))
                    except ValueError:
                        return None, None, "invalid_numeric"
                candidates.append(value)
            return self._unique_candidate(candidates, "final_answer")

        boxed_contents = _BOXED_RE.findall(text)
        if "\\boxed" in text:
            if not boxed_contents:
                return None, None, "invalid_boxed_numeric"
            boxed_candidates: list[Decimal] = []
            for content in boxed_contents:
                try:
                    boxed_candidates.append(_parse_decimal_token(content))
                except ValueError:
                    if _NON_FINITE_RE.search(content):
                        return None, None, "non_finite_number"
                    return None, None, "invalid_boxed_numeric"
            return self._unique_candidate(boxed_candidates, "boxed")

        if _NON_FINITE_RE.search(text):
            return None, None, "non_finite_number"
        if _EXPRESSION_RE.search(text):
            return None, None, "invalid_numeric_expression"

        full_match = _NUMBER_FULL_RE.fullmatch(text)
        if full_match:
            try:
                return _parse_decimal_token(full_match.group(1)), "bare_number", None
            except ValueError:
                return None, None, "invalid_numeric"

        candidates: list[Decimal] = []
        for match in _NUMBER_TOKEN_RE.finditer(text):
            try:
                candidates.append(_parse_decimal_token(match.group(1)))
            except ValueError:
                return None, None, "invalid_numeric"
        if not candidates:
            return None, None, "numeric_not_found"
        return self._unique_candidate(candidates, "unique_number")

    @staticmethod
    def _unique_candidate(
        candidates: list[Decimal], parser: str
    ) -> tuple[Decimal | None, str | None, str | None]:
        unique = set(candidates)
        if len(unique) != 1:
            return None, None, "ambiguous_numeric"
        return next(iter(unique)), parser, None

    def _error(
        self,
        parse_error: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvaluationResult:
        return EvaluationResult(
            parsed_answer=None,
            score=0.0,
            correct=False,
            evaluator_name=self.evaluator_name,
            metadata=metadata or {},
            parse_error=parse_error,
        )
