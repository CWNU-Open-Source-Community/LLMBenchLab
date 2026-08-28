"""Strict, side-effect-free loading for LLMBenchLab dataset v1.

The loader deliberately stops at validated Python values.  Persisting a
benchmark is an API/database concern and must happen in a transaction outside
this module.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import string
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = "llmbenchlab-dataset-v1"
MANIFEST_FILENAME = "manifest.json"
QUESTIONS_FILENAME = "questions.jsonl"

MIB = 1024 * 1024
MAX_MANIFEST_BYTES = 1 * MIB
MAX_QUESTIONS_BYTES = 128 * MIB
MAX_JSONL_LINE_BYTES = 256 * 1024
MAX_QUESTIONS = 20_000
MAX_ARCHIVE_BYTES = 130 * MIB
MAX_COMPRESSION_RATIO = 100.0

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "name",
        "version",
        "description",
        "dimension",
        "language",
        "license",
        "source",
        "evaluator",
        "prompt_template",
        "question_count",
    }
)
_EXPECTED_EVALUATORS = {
    "exact_match": "exact_match_v1",
    "multiple_choice": "multiple_choice_v1",
    "numeric": "numeric_v1",
}
_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?\Z")
_DIMENSION_RE = re.compile(r"[a-z][a-z0-9_-]*\Z")
_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*\Z")
_QUESTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_CHOICE_KEY_RE = re.compile(r"[A-Z]\Z")
_NUMERIC_STRING_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_DRIVE_PREFIX_RE = re.compile(r"[A-Za-z]:")


@dataclass(frozen=True, slots=True)
class DatasetLimits:
    """Resource limits used before and during parsing/decompression."""

    max_manifest_bytes: int = MAX_MANIFEST_BYTES
    max_questions_bytes: int = MAX_QUESTIONS_BYTES
    max_line_bytes: int = MAX_JSONL_LINE_BYTES
    max_questions: int = MAX_QUESTIONS
    max_archive_bytes: int = MAX_ARCHIVE_BYTES
    max_compression_ratio: float = MAX_COMPRESSION_RATIO
    max_issues: int = 100

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_manifest_bytes,
            self.max_questions_bytes,
            self.max_line_bytes,
            self.max_questions,
            self.max_archive_bytes,
            self.max_issues,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integer_limits
        ):
            raise ValueError("dataset limits must be positive integers")
        if (
            isinstance(self.max_compression_ratio, bool)
            or not isinstance(self.max_compression_ratio, (int, float))
            or not math.isfinite(self.max_compression_ratio)
            or self.max_compression_ratio <= 0
        ):
            raise ValueError("max_compression_ratio must be finite and positive")


DEFAULT_LIMITS = DatasetLimits()


@dataclass(frozen=True, slots=True)
class DatasetIssue:
    """A safe, machine-readable dataset validation issue."""

    file: str
    code: str
    message: str
    line: int | None = None
    column: int | None = None
    pointer: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "file": self.file,
            "code": self.code,
            "message": self.message,
        }
        if self.line is not None:
            result["line"] = self.line
        if self.column is not None:
            result["column"] = self.column
        if self.pointer is not None:
            result["pointer"] = self.pointer
        return result

    def location(self) -> str:
        location = self.file
        if self.line is not None:
            location += f":{self.line}"
            if self.column is not None:
                location += f":{self.column}"
        return location


class DatasetValidationError(ValueError):
    """Raised when no dataset can be produced from the supplied input."""

    def __init__(self, issues: DatasetIssue | Iterable[DatasetIssue]) -> None:
        normalized = (issues,) if isinstance(issues, DatasetIssue) else tuple(issues)
        if not normalized:
            raise ValueError("DatasetValidationError requires at least one issue")
        self.issues = normalized
        first = normalized[0]
        suffix = f" (+{len(normalized) - 1} more)" if len(normalized) > 1 else ""
        super().__init__(f"{first.location()} [{first.code}] {first.message}{suffix}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": "dataset_validation_error",
                "message": "Benchmark dataset validation failed",
                "issues": [issue.as_dict() for issue in self.issues],
            }
        }


@dataclass(frozen=True, slots=True)
class LoadedDataset:
    """Validated source values plus their deterministic content identity."""

    manifest: dict[str, Any]
    questions: tuple[dict[str, Any], ...]
    dataset_hash: str


class _IssueCollector:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.issues: list[DatasetIssue] = []
        self.truncated = False

    def add(
        self,
        file: str,
        code: str,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
        pointer: str | None = None,
    ) -> None:
        if len(self.issues) < self.limit:
            self.issues.append(
                DatasetIssue(
                    file=file,
                    code=code,
                    message=message,
                    line=line,
                    column=column,
                    pointer=pointer,
                )
            )
        else:
            self.truncated = True

    def raise_if_any(self) -> None:
        if not self.issues:
            return
        if self.truncated:
            self.issues[-1] = DatasetIssue(
                file="dataset",
                code="too_many_issues",
                message=f"Validation stopped after {self.limit} issues",
            )
        raise DatasetValidationError(self.issues)


class _DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


class _InvalidConstantError(ValueError):
    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(token)


class _InvalidUnicodeError(ValueError):
    def __init__(self, pointer: str, message: str) -> None:
        self.pointer = pointer
        super().__init__(message)


def _json_pointer(*parts: object) -> str:
    if not parts:
        return ""
    escaped = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped)


def _join_pointer(base: str, part: object) -> str:
    suffix = _json_pointer(part)
    return f"{base}{suffix}" if base else suffix


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_json_constant(token: str) -> None:
    raise _InvalidConstantError(token)


def _normalize_unicode_string(value: str, pointer: str) -> str:
    """Combine escaped UTF-16 pairs and reject unpaired surrogate values."""

    output: list[str] = []
    index = 0
    while index < len(value):
        codepoint = ord(value[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 >= len(value):
                raise _InvalidUnicodeError(pointer, "String contains an unpaired high surrogate")
            low = ord(value[index + 1])
            if not 0xDC00 <= low <= 0xDFFF:
                raise _InvalidUnicodeError(pointer, "String contains an unpaired high surrogate")
            scalar = 0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)
            output.append(chr(scalar))
            index += 2
            continue
        if 0xDC00 <= codepoint <= 0xDFFF:
            raise _InvalidUnicodeError(pointer, "String contains an unpaired low surrogate")
        output.append(value[index])
        index += 1
    return "".join(output)


def _normalize_json_unicode(value: Any, pointer: str = "") -> Any:
    if isinstance(value, str):
        return _normalize_unicode_string(value, pointer)
    if isinstance(value, list):
        return [
            _normalize_json_unicode(item, _join_pointer(pointer, index))
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            # Do not put an invalid surrogate-bearing key into an error
            # pointer; the resulting error must itself always be serializable.
            key = _normalize_unicode_string(raw_key, pointer)
            if key in result:
                raise _InvalidUnicodeError(
                    pointer,
                    "Object contains keys that are duplicates after Unicode decoding",
                )
            result[key] = _normalize_json_unicode(item, _join_pointer(pointer, key))
        return result
    return value


def _line_column(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    previous_newline = text.rfind("\n", 0, offset)
    return line, offset - previous_newline


def _decode_utf8(data: bytes, filename: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        prefix = data[: exc.start]
        line = prefix.count(b"\n") + 1
        previous_newline = prefix.rfind(b"\n")
        line_prefix = prefix[previous_newline + 1 :]
        column = len(line_prefix.decode("utf-8-sig")) + 1
        raise DatasetValidationError(
            DatasetIssue(
                file=filename,
                line=line,
                column=column,
                code="invalid_utf8",
                message="File is not valid UTF-8",
            )
        ) from exc


def _parse_json_object(
    text: str,
    filename: str,
    *,
    source_line: int | None = None,
) -> dict[str, Any]:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        line = (source_line + exc.lineno - 1) if source_line is not None else exc.lineno
        raise DatasetValidationError(
            DatasetIssue(
                file=filename,
                line=line,
                column=exc.colno,
                code="invalid_json",
                message=exc.msg,
            )
        ) from exc
    except _DuplicateKeyError as exc:
        raise DatasetValidationError(
            DatasetIssue(
                file=filename,
                line=source_line or 1,
                code="duplicate_json_key",
                message="JSON object contains a duplicate key",
            )
        ) from exc
    except _InvalidConstantError as exc:
        offset = text.find(exc.token)
        local_line, column = _line_column(text, max(offset, 0))
        line = (source_line + local_line - 1) if source_line is not None else local_line
        raise DatasetValidationError(
            DatasetIssue(
                file=filename,
                line=line,
                column=column,
                code="non_finite_number",
                message=f"Non-standard JSON number {exc.token!r} is not allowed",
            )
        ) from exc
    except (OverflowError, ValueError) as exc:
        raise DatasetValidationError(
            DatasetIssue(
                file=filename,
                line=source_line or 1,
                code="invalid_json_number",
                message="JSON contains a number that exceeds safe parser limits",
            )
        ) from exc
    except RecursionError as exc:
        raise DatasetValidationError(
            DatasetIssue(
                file=filename,
                line=source_line or 1,
                code="json_nesting_too_deep",
                message="JSON nesting exceeds the safe parser limit",
            )
        ) from exc

    try:
        value = _normalize_json_unicode(value)
    except _InvalidUnicodeError as exc:
        raise DatasetValidationError(
            DatasetIssue(
                file=filename,
                line=source_line or 1,
                pointer=exc.pointer or None,
                code="invalid_unicode",
                message=str(exc),
            )
        ) from exc
    except RecursionError as exc:
        raise DatasetValidationError(
            DatasetIssue(
                file=filename,
                line=source_line or 1,
                code="json_nesting_too_deep",
                message="JSON nesting exceeds the safe parser limit",
            )
        ) from exc

    if not isinstance(value, dict):
        raise DatasetValidationError(
            DatasetIssue(
                file=filename,
                line=source_line or 1,
                pointer="",
                code="object_required",
                message="Top-level JSON value must be an object",
            )
        )
    return value


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _add_required_and_extra_issues(
    value: Mapping[str, Any],
    *,
    required: set[str] | frozenset[str],
    allowed: set[str] | frozenset[str],
    collector: _IssueCollector,
    filename: str,
    line: int | None = None,
    base_pointer: str = "",
) -> None:
    for key in sorted(required - value.keys()):
        collector.add(
            filename,
            "required_field_missing",
            f"Required field {key!r} is missing",
            line=line,
            pointer=_join_pointer(base_pointer, key),
        )
    for key in sorted(value.keys() - allowed):
        collector.add(
            filename,
            "additional_field_not_allowed",
            f"Field {key!r} is not allowed by dataset v1",
            line=line,
            pointer=_join_pointer(base_pointer, key),
        )


def _validate_string(
    value: Any,
    *,
    collector: _IssueCollector,
    filename: str,
    pointer: str,
    line: int | None = None,
    minimum: int = 0,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> bool:
    if not isinstance(value, str):
        collector.add(
            filename,
            "invalid_type",
            "Value must be a string",
            line=line,
            pointer=pointer,
        )
        return False
    if len(value) < minimum or len(value) > maximum:
        collector.add(
            filename,
            "string_length_out_of_range",
            f"String length must be between {minimum} and {maximum}",
            line=line,
            pointer=pointer,
        )
        return False
    if pattern is not None and pattern.fullmatch(value) is None:
        collector.add(
            filename,
            "pattern_mismatch",
            "String does not match the required format",
            line=line,
            pointer=pointer,
        )
        return False
    return True


def _validate_prompt_template(template: Any, collector: _IssueCollector) -> None:
    pointer = "/prompt_template"
    if not isinstance(template, dict):
        collector.add(
            MANIFEST_FILENAME,
            "invalid_type",
            "prompt_template must be an object",
            pointer=pointer,
        )
        return
    _add_required_and_extra_issues(
        template,
        required={"system", "user"},
        allowed={"system", "user"},
        collector=collector,
        filename=MANIFEST_FILENAME,
        base_pointer=pointer,
    )
    if "system" in template:
        _validate_string(
            template["system"],
            collector=collector,
            filename=MANIFEST_FILENAME,
            pointer="/prompt_template/system",
            maximum=4_000,
        )
    if "user" not in template:
        return
    user_valid = _validate_string(
        template["user"],
        collector=collector,
        filename=MANIFEST_FILENAME,
        pointer="/prompt_template/user",
        minimum=1,
        maximum=12_000,
    )
    if not user_valid:
        return

    seen_fields: list[str] = []
    try:
        for _, field_name, format_spec, conversion in string.Formatter().parse(template["user"]):
            if field_name is None:
                continue
            if field_name not in {"prompt", "choices"}:
                collector.add(
                    MANIFEST_FILENAME,
                    "unsupported_prompt_placeholder",
                    "Prompt template contains an unsupported placeholder",
                    pointer="/prompt_template/user",
                )
                continue
            if format_spec or conversion:
                collector.add(
                    MANIFEST_FILENAME,
                    "unsupported_prompt_expression",
                    "Prompt placeholders cannot use conversion or format expressions",
                    pointer="/prompt_template/user",
                )
            seen_fields.append(field_name)
    except ValueError as exc:
        collector.add(
            MANIFEST_FILENAME,
            "invalid_prompt_template",
            f"Prompt template has unmatched braces: {exc}",
            pointer="/prompt_template/user",
        )
        return
    if "prompt" not in seen_fields:
        collector.add(
            MANIFEST_FILENAME,
            "required_prompt_placeholder_missing",
            "prompt_template.user must contain {prompt}",
            pointer="/prompt_template/user",
        )


def _validate_evaluator(evaluator: Any, collector: _IssueCollector) -> None:
    pointer = "/evaluator"
    if not isinstance(evaluator, dict):
        collector.add(
            MANIFEST_FILENAME,
            "invalid_type",
            "evaluator must be an object",
            pointer=pointer,
        )
        return
    _add_required_and_extra_issues(
        evaluator,
        required={"name", "version", "mapping"},
        allowed={"name", "version", "mapping"},
        collector=collector,
        filename=MANIFEST_FILENAME,
        base_pointer=pointer,
    )
    if "name" in evaluator and evaluator["name"] != "builtin-objective":
        collector.add(
            MANIFEST_FILENAME,
            "unsupported_evaluator",
            "evaluator.name must be 'builtin-objective' for dataset v1",
            pointer="/evaluator/name",
        )
    if "version" in evaluator and evaluator["version"] != "1.0":
        collector.add(
            MANIFEST_FILENAME,
            "unsupported_evaluator_version",
            "evaluator.version must be '1.0' for dataset v1",
            pointer="/evaluator/version",
        )
    if "mapping" not in evaluator:
        return
    mapping = evaluator["mapping"]
    if not isinstance(mapping, dict):
        collector.add(
            MANIFEST_FILENAME,
            "invalid_type",
            "evaluator.mapping must be an object",
            pointer="/evaluator/mapping",
        )
        return
    mapping_keys = set(_EXPECTED_EVALUATORS)
    _add_required_and_extra_issues(
        mapping,
        required=mapping_keys,
        allowed=mapping_keys,
        collector=collector,
        filename=MANIFEST_FILENAME,
        base_pointer="/evaluator/mapping",
    )
    for question_type, expected in _EXPECTED_EVALUATORS.items():
        if question_type in mapping and mapping[question_type] != expected:
            collector.add(
                MANIFEST_FILENAME,
                "incompatible_evaluator_mapping",
                f"{question_type} must map to {expected!r}",
                pointer=f"/evaluator/mapping/{question_type}",
            )


def _validate_manifest_value(
    manifest: Any,
    collector: _IssueCollector,
    limits: DatasetLimits,
) -> None:
    if not isinstance(manifest, dict):
        collector.add(
            MANIFEST_FILENAME,
            "object_required",
            "Manifest must be a JSON object",
            pointer="",
        )
        return
    _add_required_and_extra_issues(
        manifest,
        required=_MANIFEST_FIELDS,
        allowed=_MANIFEST_FIELDS,
        collector=collector,
        filename=MANIFEST_FILENAME,
    )

    if "schema_version" in manifest and manifest["schema_version"] != SCHEMA_VERSION:
        collector.add(
            MANIFEST_FILENAME,
            "unsupported_schema_version",
            f"schema_version must be {SCHEMA_VERSION!r}",
            pointer="/schema_version",
        )

    string_rules: dict[str, tuple[int, int, re.Pattern[str] | None]] = {
        "id": (1, 80, _ID_RE),
        "name": (1, 160, None),
        "version": (1, 64, _VERSION_RE),
        "description": (1, 4_000, None),
        "dimension": (1, 64, _DIMENSION_RE),
        "language": (1, 35, _LANGUAGE_RE),
        "license": (1, 128, None),
        "source": (1, 2_048, None),
    }
    for field, (minimum, maximum, pattern) in string_rules.items():
        if field in manifest:
            _validate_string(
                manifest[field],
                collector=collector,
                filename=MANIFEST_FILENAME,
                pointer=f"/{field}",
                minimum=minimum,
                maximum=maximum,
                pattern=pattern,
            )

    if "question_count" in manifest:
        count = manifest["question_count"]
        maximum = min(MAX_QUESTIONS, limits.max_questions)
        if not _is_integer(count):
            collector.add(
                MANIFEST_FILENAME,
                "invalid_type",
                "question_count must be an integer",
                pointer="/question_count",
            )
        elif not 1 <= count <= maximum:
            collector.add(
                MANIFEST_FILENAME,
                "question_count_out_of_range",
                f"question_count must be between 1 and {maximum}",
                pointer="/question_count",
            )

    if "evaluator" in manifest:
        _validate_evaluator(manifest["evaluator"], collector)
    if "prompt_template" in manifest:
        _validate_prompt_template(manifest["prompt_template"], collector)


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    limits: DatasetLimits = DEFAULT_LIMITS,
) -> None:
    """Validate a decoded source manifest, raising one structured error."""

    collector = _IssueCollector(limits.max_issues)
    _validate_manifest_value(manifest, collector, limits)
    collector.raise_if_any()


def parse_manifest_bytes(
    data: bytes,
    *,
    limits: DatasetLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Decode, strictly parse, and validate ``manifest.json`` bytes."""

    if not isinstance(data, bytes):
        raise TypeError("manifest data must be bytes")
    if len(data) > limits.max_manifest_bytes:
        raise DatasetValidationError(
            DatasetIssue(
                file=MANIFEST_FILENAME,
                code="file_too_large",
                message=f"File exceeds the {limits.max_manifest_bytes}-byte limit",
            )
        )
    text = _decode_utf8(data, MANIFEST_FILENAME)
    manifest = _parse_json_object(text, MANIFEST_FILENAME)
    validate_manifest(manifest, limits=limits)
    return manifest


def _validate_common_question_fields(
    question: Mapping[str, Any],
    collector: _IssueCollector,
    line: int,
) -> None:
    if "id" in question:
        _validate_string(
            question["id"],
            collector=collector,
            filename=QUESTIONS_FILENAME,
            line=line,
            pointer="/id",
            minimum=1,
            maximum=128,
            pattern=_QUESTION_ID_RE,
        )
    if "prompt" in question:
        _validate_string(
            question["prompt"],
            collector=collector,
            filename=QUESTIONS_FILENAME,
            line=line,
            pointer="/prompt",
            minimum=1,
            maximum=20_000,
        )
    if "metadata" in question and not isinstance(question["metadata"], dict):
        collector.add(
            QUESTIONS_FILENAME,
            "invalid_type",
            "metadata must be an object",
            line=line,
            pointer="/metadata",
        )


def _validate_exact_match(
    question: Mapping[str, Any],
    collector: _IssueCollector,
    line: int,
) -> None:
    allowed = {"id", "type", "prompt", "answer", "evaluator_config", "metadata"}
    required = {"id", "type", "prompt", "answer", "metadata"}
    _add_required_and_extra_issues(
        question,
        required=required,
        allowed=allowed,
        collector=collector,
        filename=QUESTIONS_FILENAME,
        line=line,
    )
    _validate_common_question_fields(question, collector, line)
    if "answer" in question:
        _validate_string(
            question["answer"],
            collector=collector,
            filename=QUESTIONS_FILENAME,
            line=line,
            pointer="/answer",
            maximum=4_000,
        )
    if "evaluator_config" not in question:
        return
    config = question["evaluator_config"]
    if not isinstance(config, dict):
        collector.add(
            QUESTIONS_FILENAME,
            "invalid_type",
            "evaluator_config must be an object",
            line=line,
            pointer="/evaluator_config",
        )
        return
    _add_required_and_extra_issues(
        config,
        required=set(),
        allowed={"case_sensitive", "normalize_whitespace"},
        collector=collector,
        filename=QUESTIONS_FILENAME,
        line=line,
        base_pointer="/evaluator_config",
    )
    for field in ("case_sensitive", "normalize_whitespace"):
        if field in config and not isinstance(config[field], bool):
            collector.add(
                QUESTIONS_FILENAME,
                "invalid_type",
                f"{field} must be a boolean",
                line=line,
                pointer=f"/evaluator_config/{field}",
            )


def _validate_multiple_choice(
    question: Mapping[str, Any],
    collector: _IssueCollector,
    line: int,
) -> None:
    allowed = {"id", "type", "prompt", "choices", "answer", "metadata"}
    required = allowed
    _add_required_and_extra_issues(
        question,
        required=required,
        allowed=allowed,
        collector=collector,
        filename=QUESTIONS_FILENAME,
        line=line,
    )
    _validate_common_question_fields(question, collector, line)
    choices = question.get("choices")
    choices_valid = isinstance(choices, dict)
    if not choices_valid:
        if "choices" in question:
            collector.add(
                QUESTIONS_FILENAME,
                "invalid_type",
                "choices must be an object",
                line=line,
                pointer="/choices",
            )
    else:
        if not 2 <= len(choices) <= 26:
            collector.add(
                QUESTIONS_FILENAME,
                "choices_count_out_of_range",
                "choices must contain between 2 and 26 entries",
                line=line,
                pointer="/choices",
            )
        for key, value in choices.items():
            key_pointer = _join_pointer("/choices", key)
            if _CHOICE_KEY_RE.fullmatch(key) is None:
                collector.add(
                    QUESTIONS_FILENAME,
                    "invalid_choice_key",
                    "Choice keys must be one uppercase letter A-Z",
                    line=line,
                    pointer=key_pointer,
                )
            _validate_string(
                value,
                collector=collector,
                filename=QUESTIONS_FILENAME,
                line=line,
                pointer=key_pointer,
                minimum=1,
                maximum=4_000,
            )

    answer_valid = False
    if "answer" in question:
        answer_valid = _validate_string(
            question["answer"],
            collector=collector,
            filename=QUESTIONS_FILENAME,
            line=line,
            pointer="/answer",
            minimum=1,
            maximum=1,
            pattern=_CHOICE_KEY_RE,
        )
    if answer_valid and choices_valid and question["answer"] not in choices:
        collector.add(
            QUESTIONS_FILENAME,
            "answer_not_in_choices",
            "multiple_choice answer must name an existing choice",
            line=line,
            pointer="/answer",
        )


def _validate_numeric(
    question: Mapping[str, Any],
    collector: _IssueCollector,
    line: int,
) -> None:
    allowed = {"id", "type", "prompt", "answer", "evaluator_config", "metadata"}
    required = allowed
    _add_required_and_extra_issues(
        question,
        required=required,
        allowed=allowed,
        collector=collector,
        filename=QUESTIONS_FILENAME,
        line=line,
    )
    _validate_common_question_fields(question, collector, line)

    if "answer" in question:
        answer = question["answer"]
        valid = False
        if _is_number(answer):
            valid = not isinstance(answer, float) or math.isfinite(answer)
        elif (
            isinstance(answer, str) and len(answer) <= 128 and _NUMERIC_STRING_RE.fullmatch(answer)
        ):
            try:
                valid = Decimal(answer).is_finite()
            except InvalidOperation:
                valid = False
        if not valid:
            collector.add(
                QUESTIONS_FILENAME,
                "invalid_numeric_answer",
                "numeric answer must be a finite number or decimal string",
                line=line,
                pointer="/answer",
            )

    if "evaluator_config" not in question:
        return
    config = question["evaluator_config"]
    if not isinstance(config, dict):
        collector.add(
            QUESTIONS_FILENAME,
            "invalid_type",
            "evaluator_config must be an object",
            line=line,
            pointer="/evaluator_config",
        )
        return
    tolerance_fields = {"absolute_tolerance", "relative_tolerance"}
    _add_required_and_extra_issues(
        config,
        required=set(),
        allowed=tolerance_fields,
        collector=collector,
        filename=QUESTIONS_FILENAME,
        line=line,
        base_pointer="/evaluator_config",
    )
    for field in sorted(tolerance_fields):
        if field not in config:
            continue
        value = config[field]
        if not _is_number(value):
            collector.add(
                QUESTIONS_FILENAME,
                "invalid_type",
                f"{field} must be a number",
                line=line,
                pointer=f"/evaluator_config/{field}",
            )
        elif (isinstance(value, float) and not math.isfinite(value)) or value < 0:
            collector.add(
                QUESTIONS_FILENAME,
                "invalid_tolerance",
                f"{field} must be finite and non-negative",
                line=line,
                pointer=f"/evaluator_config/{field}",
            )


def _find_non_finite(value: Any, pointer: str = "") -> str | None:
    if isinstance(value, float) and not math.isfinite(value):
        return pointer
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _find_non_finite(item, _join_pointer(pointer, index))
            if found is not None:
                return found
    elif isinstance(value, dict):
        for key, item in value.items():
            found = _find_non_finite(item, _join_pointer(pointer, key))
            if found is not None:
                return found
    return None


def _validate_question_value(
    question: Any,
    collector: _IssueCollector,
    line: int,
) -> None:
    if not isinstance(question, dict):
        collector.add(
            QUESTIONS_FILENAME,
            "object_required",
            "Each JSONL line must contain one JSON object",
            line=line,
            pointer="",
        )
        return
    non_finite_pointer = _find_non_finite(question)
    if non_finite_pointer is not None:
        collector.add(
            QUESTIONS_FILENAME,
            "non_finite_number",
            "Numbers must be finite",
            line=line,
            pointer=non_finite_pointer,
        )
    question_type = question.get("type")
    if "type" not in question:
        collector.add(
            QUESTIONS_FILENAME,
            "required_field_missing",
            "Required field 'type' is missing",
            line=line,
            pointer="/type",
        )
        return
    if question_type == "exact_match":
        _validate_exact_match(question, collector, line)
    elif question_type == "multiple_choice":
        _validate_multiple_choice(question, collector, line)
    elif question_type == "numeric":
        _validate_numeric(question, collector, line)
    else:
        collector.add(
            QUESTIONS_FILENAME,
            "unsupported_question_type",
            "type must be exact_match, multiple_choice, or numeric",
            line=line,
            pointer="/type",
        )


def _validate_cross_fields(
    manifest: Mapping[str, Any],
    questions_with_lines: Sequence[tuple[int, dict[str, Any]]],
    record_count: int,
    collector: _IssueCollector,
) -> None:
    seen_ids: dict[str, int] = {}
    mapping: Mapping[str, Any] = {}
    evaluator = manifest.get("evaluator")
    if isinstance(evaluator, dict) and isinstance(evaluator.get("mapping"), dict):
        mapping = evaluator["mapping"]

    for line, question in questions_with_lines:
        question_id = question.get("id")
        if isinstance(question_id, str):
            if question_id in seen_ids:
                collector.add(
                    QUESTIONS_FILENAME,
                    "duplicate_question_id",
                    f"Question ID duplicates the ID on line {seen_ids[question_id]}",
                    line=line,
                    pointer="/id",
                )
            else:
                seen_ids[question_id] = line
        question_type = question.get("type")
        if question_type in _EXPECTED_EVALUATORS:
            mapped = mapping.get(question_type)
            if mapped != _EXPECTED_EVALUATORS[question_type]:
                collector.add(
                    QUESTIONS_FILENAME,
                    "incompatible_evaluator_mapping",
                    f"No compatible evaluator mapping for {question_type}",
                    line=line,
                    pointer="/type",
                )

    declared_count = manifest.get("question_count")
    if _is_integer(declared_count) and declared_count != record_count:
        collector.add(
            MANIFEST_FILENAME,
            "question_count_mismatch",
            "question_count is "
            f"{declared_count}, but questions.jsonl contains {record_count} records",
            pointer="/question_count",
        )


def parse_questions_jsonl(
    data: bytes,
    *,
    manifest: Mapping[str, Any] | None = None,
    limits: DatasetLimits = DEFAULT_LIMITS,
) -> tuple[dict[str, Any], ...]:
    """Strictly parse and validate question JSONL, preserving record order."""

    if not isinstance(data, bytes):
        raise TypeError("questions data must be bytes")
    if len(data) > limits.max_questions_bytes:
        raise DatasetValidationError(
            DatasetIssue(
                file=QUESTIONS_FILENAME,
                code="file_too_large",
                message=f"File exceeds the {limits.max_questions_bytes}-byte limit",
            )
        )
    text = _decode_utf8(data, QUESTIONS_FILENAME)
    for index, char in enumerate(text):
        if char == "\r" and (index + 1 >= len(text) or text[index + 1] != "\n"):
            line, column = _line_column(text, index)
            raise DatasetValidationError(
                DatasetIssue(
                    file=QUESTIONS_FILENAME,
                    line=line,
                    column=column,
                    code="invalid_line_ending",
                    message="Only LF and CRLF line endings are allowed",
                )
            )

    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    if len(raw_lines) > limits.max_questions:
        raise DatasetValidationError(
            DatasetIssue(
                file=QUESTIONS_FILENAME,
                line=limits.max_questions + 1,
                code="too_many_questions",
                message=f"Dataset exceeds the {limits.max_questions}-question limit",
            )
        )

    collector = _IssueCollector(limits.max_issues)
    parsed: list[tuple[int, dict[str, Any]]] = []
    for line_number, raw_line in enumerate(raw_lines, start=1):
        line_text = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        line_size = len(line_text.encode("utf-8"))
        if line_size > limits.max_line_bytes:
            collector.add(
                QUESTIONS_FILENAME,
                "line_too_large",
                f"Line exceeds the {limits.max_line_bytes}-byte limit",
                line=line_number,
            )
            continue
        if not line_text.strip():
            collector.add(
                QUESTIONS_FILENAME,
                "empty_jsonl_line",
                "Empty lines are not allowed in questions.jsonl",
                line=line_number,
                column=1,
            )
            continue
        try:
            question = _parse_json_object(
                line_text,
                QUESTIONS_FILENAME,
                source_line=line_number,
            )
        except DatasetValidationError as exc:
            for issue in exc.issues:
                collector.add(
                    issue.file,
                    issue.code,
                    issue.message,
                    line=issue.line,
                    column=issue.column,
                    pointer=issue.pointer,
                )
            continue
        parsed.append((line_number, question))
        _validate_question_value(question, collector, line_number)

    if not raw_lines:
        collector.add(
            QUESTIONS_FILENAME,
            "no_questions",
            "questions.jsonl must contain at least one record",
            line=1,
        )

    if manifest is not None:
        _validate_cross_fields(manifest, parsed, len(parsed), collector)
    else:
        seen_ids: dict[str, int] = {}
        for line, question in parsed:
            question_id = question.get("id")
            if isinstance(question_id, str):
                if question_id in seen_ids:
                    collector.add(
                        QUESTIONS_FILENAME,
                        "duplicate_question_id",
                        f"Question ID duplicates the ID on line {seen_ids[question_id]}",
                        line=line,
                        pointer="/id",
                    )
                else:
                    seen_ids[question_id] = line

    collector.raise_if_any()
    return tuple(question for _, question in parsed)


def validate_questions(
    questions: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    *,
    limits: DatasetLimits = DEFAULT_LIMITS,
) -> None:
    """Validate already-decoded questions and all manifest cross-fields."""

    if isinstance(questions, (str, bytes, bytearray)) or not isinstance(questions, Sequence):
        raise TypeError("questions must be a sequence of objects")
    collector = _IssueCollector(limits.max_issues)
    if len(questions) > limits.max_questions:
        collector.add(
            QUESTIONS_FILENAME,
            "too_many_questions",
            f"Dataset exceeds the {limits.max_questions}-question limit",
            line=limits.max_questions + 1,
        )
    normalized: list[tuple[int, dict[str, Any]]] = []
    for line, question in enumerate(questions, start=1):
        _validate_question_value(question, collector, line)
        if isinstance(question, dict):
            normalized.append((line, question))
    _validate_cross_fields(manifest, normalized, len(questions), collector)
    collector.raise_if_any()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def compute_dataset_hash(
    manifest: Mapping[str, Any],
    questions: Sequence[Mapping[str, Any]],
) -> str:
    """Return the dataset-v1 SHA-256 over canonical source values.

    Any importer-derived keys on ``manifest`` are intentionally removed by
    retaining only fields defined by the source schema.  Question order is
    intentionally significant, and every canonical JSON record is LF
    terminated, including the final one.
    """

    source_manifest = {key: manifest[key] for key in _MANIFEST_FIELDS if key in manifest}
    digest = hashlib.sha256()
    try:
        digest.update(_canonical_json(source_manifest).encode("utf-8"))
        digest.update(b"\n")
        for question in questions:
            digest.update(_canonical_json(question).encode("utf-8"))
            digest.update(b"\n")
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DatasetValidationError(
            DatasetIssue(
                file="dataset",
                code="canonicalization_failed",
                message="Dataset contains a value that cannot be canonicalized",
            )
        ) from exc
    return digest.hexdigest()


def load_dataset_bytes(
    manifest_data: bytes,
    questions_data: bytes,
    *,
    limits: DatasetLimits = DEFAULT_LIMITS,
) -> LoadedDataset:
    """Load a dataset from its two in-memory source files."""

    manifest = parse_manifest_bytes(manifest_data, limits=limits)
    questions = parse_questions_jsonl(questions_data, manifest=manifest, limits=limits)
    return LoadedDataset(
        manifest=manifest,
        questions=questions,
        dataset_hash=compute_dataset_hash(manifest, questions),
    )


def _single_issue(
    file: str,
    code: str,
    message: str,
    *,
    line: int | None = None,
    column: int | None = None,
    pointer: str | None = None,
) -> DatasetValidationError:
    return DatasetValidationError(
        DatasetIssue(
            file=file,
            code=code,
            message=message,
            line=line,
            column=column,
            pointer=pointer,
        )
    )


def _read_regular_file(path: Path, filename: str, byte_limit: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _single_issue(
            filename,
            "missing_file",
            f"Required file {filename!r} is missing",
        ) from exc
    except OSError as exc:
        raise _single_issue(
            filename,
            "unsafe_file",
            f"Cannot safely open required file {filename!r}",
        ) from exc

    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise _single_issue(filename, "unsafe_file", f"{filename!r} must be a regular file")
        if file_stat.st_size > byte_limit:
            raise _single_issue(
                filename,
                "file_too_large",
                f"File exceeds the {byte_limit}-byte limit",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, byte_limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > byte_limit:
                raise _single_issue(
                    filename,
                    "file_too_large",
                    f"File exceeds the {byte_limit}-byte limit",
                )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_dataset_directory(
    directory: str | os.PathLike[str],
    *,
    limits: DatasetLimits = DEFAULT_LIMITS,
) -> LoadedDataset:
    """Load the two fixed files from a non-symlink local directory."""

    root = Path(directory)
    try:
        root_lstat = root.lstat()
    except (FileNotFoundError, OSError) as exc:
        raise _single_issue(
            "dataset",
            "invalid_dataset_directory",
            "Dataset directory is unavailable",
        ) from exc
    if stat.S_ISLNK(root_lstat.st_mode) or not stat.S_ISDIR(root_lstat.st_mode):
        raise _single_issue(
            "dataset",
            "invalid_dataset_directory",
            "Dataset root must be a real directory, not a symlink",
        )
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise _single_issue(
            "dataset",
            "invalid_dataset_directory",
            "Dataset directory is unavailable",
        ) from exc

    paths: dict[str, Path] = {}
    for filename in (MANIFEST_FILENAME, QUESTIONS_FILENAME):
        candidate = root / filename
        try:
            candidate_lstat = candidate.lstat()
        except FileNotFoundError as exc:
            raise _single_issue(
                filename,
                "missing_file",
                f"Required file {filename!r} is missing",
            ) from exc
        except OSError as exc:
            raise _single_issue(
                filename,
                "unsafe_file",
                f"Cannot inspect required file {filename!r}",
            ) from exc
        if stat.S_ISLNK(candidate_lstat.st_mode) or not stat.S_ISREG(candidate_lstat.st_mode):
            raise _single_issue(
                filename,
                "unsafe_file",
                f"{filename!r} must be a regular non-symlink file",
            )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise _single_issue(
                filename,
                "unsafe_file",
                f"Cannot resolve required file {filename!r}",
            ) from exc
        if resolved.parent != resolved_root:
            raise _single_issue(
                filename,
                "path_traversal",
                f"{filename!r} escapes the dataset directory",
            )
        paths[filename] = candidate

    manifest_data = _read_regular_file(
        paths[MANIFEST_FILENAME],
        MANIFEST_FILENAME,
        limits.max_manifest_bytes,
    )
    questions_data = _read_regular_file(
        paths[QUESTIONS_FILENAME],
        QUESTIONS_FILENAME,
        limits.max_questions_bytes,
    )
    return load_dataset_bytes(manifest_data, questions_data, limits=limits)


def _validate_zip_name(name: str) -> None:
    portable = name.replace("\\", "/")
    path = PurePosixPath(portable)
    if (
        not name
        or "\x00" in name
        or name.startswith(("/", "\\"))
        or _DRIVE_PREFIX_RE.match(name)
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in portable.split("/"))
    ):
        raise _single_issue(
            "archive.zip",
            "unsafe_archive_path",
            "ZIP entries must use safe root-relative paths",
        )
    if portable != name or name not in {MANIFEST_FILENAME, QUESTIONS_FILENAME}:
        raise _single_issue(
            "archive.zip",
            "unexpected_archive_entry",
            "ZIP may contain only root manifest.json and questions.jsonl",
        )


def _read_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    byte_limit: int,
) -> bytes:
    try:
        with archive.open(info, "r") as member:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = member.read(min(64 * 1024, byte_limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > byte_limit:
                    raise _single_issue(
                        info.filename,
                        "file_too_large",
                        f"Expanded file exceeds the {byte_limit}-byte limit",
                    )
            if total != info.file_size:
                raise _single_issue(
                    "archive.zip",
                    "invalid_zip_metadata",
                    "ZIP member size does not match its metadata",
                )
            return b"".join(chunks)
    except DatasetValidationError:
        raise
    except (RuntimeError, NotImplementedError, OSError, EOFError, zipfile.BadZipFile) as exc:
        raise _single_issue(
            "archive.zip",
            "invalid_zip",
            "ZIP member could not be safely decompressed",
        ) from exc


def load_dataset_zip_bytes(
    archive_data: bytes,
    *,
    limits: DatasetLimits = DEFAULT_LIMITS,
) -> LoadedDataset:
    """Load a two-file ZIP with traversal, size, and bomb protections."""

    if not isinstance(archive_data, bytes):
        raise TypeError("archive data must be bytes")
    if len(archive_data) > limits.max_archive_bytes:
        raise _single_issue(
            "archive.zip",
            "archive_too_large",
            f"ZIP exceeds the {limits.max_archive_bytes}-byte limit",
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_data), mode="r")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        raise _single_issue(
            "archive.zip",
            "invalid_zip",
            "Input is not a valid ZIP archive",
        ) from exc

    with archive:
        infos = archive.infolist()
        seen: set[str] = set()
        by_name: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            # ``zipfile`` truncates ``filename`` at a NUL but keeps the source
            # spelling in ``orig_filename``.  Validate both so an ambiguous
            # central-directory name cannot masquerade as an allowed file.
            _validate_zip_name(info.orig_filename)
            _validate_zip_name(info.filename)
            if info.filename in seen:
                raise _single_issue(
                    "archive.zip",
                    "duplicate_archive_entry",
                    f"ZIP contains duplicate entry {info.filename!r}",
                )
            seen.add(info.filename)
            by_name[info.filename] = info

            if info.is_dir():
                raise _single_issue(
                    "archive.zip",
                    "unexpected_archive_entry",
                    "ZIP directory entries are not allowed",
                )
            mode_type = stat.S_IFMT(info.external_attr >> 16)
            if mode_type not in {0, stat.S_IFREG}:
                raise _single_issue(
                    "archive.zip",
                    "unsafe_archive_entry",
                    "ZIP entries must be regular files",
                )
            if info.flag_bits & 0x1:
                raise _single_issue(
                    "archive.zip",
                    "encrypted_archive_entry",
                    "Encrypted ZIP entries are not supported",
                )
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise _single_issue(
                    "archive.zip",
                    "unsupported_zip_compression",
                    "Only stored and deflated ZIP entries are supported",
                )

            member_limit = (
                limits.max_manifest_bytes
                if info.filename == MANIFEST_FILENAME
                else limits.max_questions_bytes
            )
            if info.file_size > member_limit:
                raise _single_issue(
                    info.filename,
                    "file_too_large",
                    f"Expanded file exceeds the {member_limit}-byte limit",
                )
            if info.file_size:
                if info.compress_size == 0:
                    raise _single_issue(
                        "archive.zip",
                        "compression_bomb",
                        "ZIP entry has an unsafe expansion ratio",
                    )
                ratio = info.file_size / info.compress_size
                if ratio > limits.max_compression_ratio:
                    raise _single_issue(
                        "archive.zip",
                        "compression_bomb",
                        "ZIP entry exceeds the permitted expansion ratio",
                    )

        required = {MANIFEST_FILENAME, QUESTIONS_FILENAME}
        if seen != required or len(infos) != 2:
            missing = sorted(required - seen)
            message = "ZIP must contain exactly manifest.json and questions.jsonl"
            if missing:
                message += f"; missing {', '.join(missing)}"
            raise _single_issue("archive.zip", "invalid_archive_contents", message)

        total_expanded = sum(info.file_size for info in infos)
        if total_expanded > limits.max_manifest_bytes + limits.max_questions_bytes:
            raise _single_issue(
                "archive.zip",
                "archive_expands_too_large",
                "ZIP expanded content exceeds the combined dataset limit",
            )

        manifest_data = _read_zip_member(
            archive,
            by_name[MANIFEST_FILENAME],
            limits.max_manifest_bytes,
        )
        questions_data = _read_zip_member(
            archive,
            by_name[QUESTIONS_FILENAME],
            limits.max_questions_bytes,
        )
    return load_dataset_bytes(manifest_data, questions_data, limits=limits)


class DatasetLoader:
    """Convenience facade for callers that want reusable custom limits."""

    def __init__(self, limits: DatasetLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def load_directory(self, directory: str | os.PathLike[str]) -> LoadedDataset:
        return load_dataset_directory(directory, limits=self.limits)

    def load_zip_bytes(self, archive_data: bytes) -> LoadedDataset:
        return load_dataset_zip_bytes(archive_data, limits=self.limits)

    def load_bytes(self, manifest_data: bytes, questions_data: bytes) -> LoadedDataset:
        return load_dataset_bytes(manifest_data, questions_data, limits=self.limits)


# Readable aliases retained for integration code and third-party callers.
load_dataset = load_dataset_directory
load_dataset_from_directory = load_dataset_directory
load_dataset_from_zip = load_dataset_zip_bytes
calculate_dataset_hash = compute_dataset_hash


__all__ = [
    "DEFAULT_LIMITS",
    "MANIFEST_FILENAME",
    "MAX_ARCHIVE_BYTES",
    "MAX_COMPRESSION_RATIO",
    "MAX_JSONL_LINE_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_QUESTIONS",
    "MAX_QUESTIONS_BYTES",
    "QUESTIONS_FILENAME",
    "SCHEMA_VERSION",
    "DatasetIssue",
    "DatasetLimits",
    "DatasetLoader",
    "DatasetValidationError",
    "LoadedDataset",
    "calculate_dataset_hash",
    "compute_dataset_hash",
    "load_dataset",
    "load_dataset_bytes",
    "load_dataset_directory",
    "load_dataset_from_directory",
    "load_dataset_from_zip",
    "load_dataset_zip_bytes",
    "parse_manifest_bytes",
    "parse_questions_jsonl",
    "validate_manifest",
    "validate_questions",
]
