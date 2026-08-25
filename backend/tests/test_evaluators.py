from __future__ import annotations

import pytest

from app.evaluators import (
    ExactMatchEvaluator,
    MultipleChoiceEvaluator,
    NumericEvaluator,
    get_evaluator,
)


def test_exact_match_normalizes_line_endings_and_unicode_whitespace() -> None:
    result = ExactMatchEvaluator().evaluate("  Hello\r\n\tworld  ", "hello world")

    assert result.correct is True
    assert result.score == 1
    assert result.parsed_answer == "hello world"
    assert result.parse_error is None


def test_exact_match_case_sensitivity_is_configurable() -> None:
    evaluator = ExactMatchEvaluator()

    assert evaluator.evaluate("Paris", "paris").correct is True
    assert evaluator.evaluate("Paris", "paris", {"case_sensitive": True}).correct is False


def test_exact_match_does_not_do_fuzzy_or_contains_matching() -> None:
    result = ExactMatchEvaluator().evaluate("The answer is Paris", "Paris")

    assert result.correct is False
    assert result.parse_error is None


def test_exact_match_empty_response_is_a_parse_error() -> None:
    result = ExactMatchEvaluator().evaluate(" \n ", "")

    assert result.score == 0
    assert result.parse_error == "empty_response"


@pytest.mark.parametrize(
    "response",
    [
        "A",
        "A.",
        "(A)",
        "\uff08A\uff09",
        "答案是 A",
        "The answer is A",
        "选择 A",
        "最终答案\uff1aA",
    ],
)
def test_multiple_choice_supported_formats(response: str) -> None:
    result = MultipleChoiceEvaluator().evaluate(response, "A")

    assert result.correct is True
    assert result.parsed_answer == "A"
    assert result.parse_error is None


def test_multiple_choice_explicit_answer_wins_over_reasoning_letters() -> None:
    result = MultipleChoiceEvaluator().evaluate(
        "A looks tempting and B is discussed.\n最终答案\uff1aC", "C"
    )

    assert result.correct is True
    assert result.parsed_answer == "C"


def test_multiple_choice_conflicting_explicit_answers_are_ambiguous() -> None:
    result = MultipleChoiceEvaluator().evaluate("答案是 A。The answer is B.", "A")

    assert result.score == 0
    assert result.parse_error == "ambiguous_choice"
    assert result.parsed_answer is None


@pytest.mark.parametrize("response", ["答案是 A 或 B", "The answer is A or B", "选择 A/B"])
def test_multiple_choice_explicit_alternatives_are_ambiguous(response: str) -> None:
    result = MultipleChoiceEvaluator().evaluate(response, "A")

    assert result.parse_error == "ambiguous_choice"


def test_multiple_choice_does_not_parse_first_letter_of_a_word() -> None:
    result = MultipleChoiceEvaluator().evaluate("The answer is Apple.", "A")

    assert result.parse_error == "choice_not_found"


def test_multiple_choice_conflicting_isolated_lines_are_ambiguous() -> None:
    result = MultipleChoiceEvaluator().evaluate("推理如下\nA\nB", "A")

    assert result.parse_error == "ambiguous_choice"


def test_multiple_choice_does_not_scan_random_letters_from_prose() -> None:
    result = MultipleChoiceEvaluator().evaluate("A and B both appear in this discussion.", "A")

    assert result.parse_error == "choice_not_found"


def test_multiple_choice_rejects_key_not_present_in_choices() -> None:
    result = MultipleChoiceEvaluator().evaluate(
        "最终答案\uff1aD", "A", {"choices": {"A": "one", "B": "two"}}
    )

    assert result.parse_error == "invalid_choice"
    assert result.parsed_answer == "D"


@pytest.mark.parametrize(
    ("response", "reference"),
    [
        ("42", "42"),
        ("42.0", 42),
        ("-3.5", "-3.5"),
        ("1e-3", "0.001"),
        (r"\boxed{42}", 42),
        ("最终答案是 42", 42),
        (r"最终答案是 \boxed{42}", 42),
    ],
)
def test_numeric_supported_formats(response: str, reference: object) -> None:
    result = NumericEvaluator().evaluate(response, reference)

    assert result.correct is True
    assert result.parse_error is None


def test_numeric_absolute_tolerance() -> None:
    result = NumericEvaluator().evaluate("3.141", "3.14", {"absolute_tolerance": "0.001"})

    assert result.correct is True


def test_numeric_relative_tolerance_uses_reference_magnitude() -> None:
    result = NumericEvaluator().evaluate("101", "100", {"relative_tolerance": "0.01"})

    assert result.correct is True
    assert result.metadata["allowed_tolerance"] == "1.00"


def test_numeric_final_answer_wins_over_reasoning_numbers() -> None:
    result = NumericEvaluator().evaluate("I considered 40 and 41. Final answer is 42.", 42)

    assert result.correct is True
    assert result.parsed_answer == "42"


def test_numeric_multiple_unmarked_numbers_are_ambiguous() -> None:
    result = NumericEvaluator().evaluate("It might be 41 or 42", 42)

    assert result.parse_error == "ambiguous_numeric"


def test_numeric_final_alternatives_are_ambiguous() -> None:
    result = NumericEvaluator().evaluate("The answer is 41 or 42", 42)

    assert result.parse_error == "ambiguous_numeric"


@pytest.mark.parametrize(
    "response",
    ["NaN", "Infinity", "-inf", "最终答案是 NaN", r"最终答案是 \boxed{Infinity}"],
)
def test_numeric_rejects_non_finite_values(response: str) -> None:
    result = NumericEvaluator().evaluate(response, 0)

    assert result.score == 0
    assert result.parse_error == "non_finite_number"


@pytest.mark.parametrize(
    "response",
    ["six", "6 * 7", "2+2", "1/1", "最终答案是 6*7", r"\boxed{6+7}"],
)
def test_numeric_rejects_invalid_or_expression_input(response: str) -> None:
    result = NumericEvaluator().evaluate(response, 42)

    assert result.score == 0
    assert result.parse_error is not None


def test_numeric_rejects_invalid_tolerance_and_reference() -> None:
    evaluator = NumericEvaluator()

    assert evaluator.evaluate("1", "NaN").parse_error == "invalid_reference_answer"
    assert (
        evaluator.evaluate("1", "1", {"absolute_tolerance": -1}).parse_error == "invalid_tolerance"
    )


@pytest.mark.parametrize(
    ("name", "expected_type"),
    [
        ("exact_match_v1", ExactMatchEvaluator),
        ("multiple-choice", MultipleChoiceEvaluator),
        ("numeric_v1", NumericEvaluator),
    ],
)
def test_evaluator_registry(name: str, expected_type: type[object]) -> None:
    assert isinstance(get_evaluator(name), expected_type)
