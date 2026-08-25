"""Provider-independent objective evaluation contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

EvaluatorConfig = Mapping[str, Any]


@dataclass(frozen=True)
class EvaluationResult:
    """Normalized parse and binary score for one model response."""

    parsed_answer: str | None
    score: float
    correct: bool
    evaluator_name: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    parse_error: str | None = None

    @property
    def parseable(self) -> bool:
        return self.parse_error is None


class Evaluator(ABC):
    """Deterministic evaluator interface; implementations perform no I/O."""

    evaluator_name: str

    @abstractmethod
    def evaluate(
        self,
        raw_response: object,
        reference_answer: object,
        config: EvaluatorConfig | None = None,
    ) -> EvaluationResult:
        """Parse and score a raw model response."""
