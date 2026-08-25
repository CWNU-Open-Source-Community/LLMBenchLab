from __future__ import annotations

import hashlib
import io
import json
import os
import warnings
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.services.dataset_loader import (
    DatasetLimits,
    DatasetValidationError,
    compute_dataset_hash,
    load_dataset_bytes,
    load_dataset_directory,
    load_dataset_zip_bytes,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIRECTORY = REPOSITORY_ROOT / "benchmarks" / "demo-general"


def _manifest(question_count: int = 1) -> dict[str, Any]:
    return {
        "schema_version": "llmbenchlab-dataset-v1",
        "id": "unit-demo",
        "name": "Unit Demo",
        "version": "1.0.0",
        "description": "Demo only; not a formal capability result.",
        "dimension": "general",
        "language": "mul",
        "license": "MIT",
        "source": "Original unit-test fixture",
        "evaluator": {
            "name": "builtin-objective",
            "version": "1.0",
            "mapping": {
                "exact_match": "exact_match_v1",
                "multiple_choice": "multiple_choice_v1",
                "numeric": "numeric_v1",
            },
        },
        "prompt_template": {"system": "Be brief.", "user": "{prompt}\n{choices}"},
        "question_count": question_count,
    }


def _exact(question_id: str = "q-1") -> dict[str, Any]:
    return {
        "id": question_id,
        "type": "exact_match",
        "prompt": "Write alpha.",
        "answer": "alpha",
        "metadata": {"mock_response": "alpha"},
    }


def _json_bytes(value: Any, **kwargs: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, **kwargs).encode("utf-8")


def _jsonl_bytes(*questions: dict[str, Any], newline: bytes = b"\n") -> bytes:
    return newline.join(_json_bytes(question) for question in questions) + newline


def _zip_bytes(
    entries: list[tuple[str, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, "w", compression=compression) as archive:
            for name, data in entries:
                archive.writestr(name, data)
    return output.getvalue()


def _assert_issue(error: DatasetValidationError, code: str, file: str) -> None:
    matching = [issue for issue in error.issues if issue.code == code]
    assert matching, [issue.as_dict() for issue in error.issues]
    assert matching[0].file == file


def test_builtin_demo_is_valid_bilingual_and_explicitly_non_formal() -> None:
    loaded = load_dataset_directory(DEMO_DIRECTORY)

    assert loaded.manifest["id"] == "demo-general"
    assert loaded.manifest["language"] == "mul"
    assert loaded.manifest["question_count"] == len(loaded.questions) == 15
    assert "Demo 数据" in loaded.manifest["description"]
    assert "不代表正式模型能力" in loaded.manifest["description"]
    assert {question["type"] for question in loaded.questions} == {
        "exact_match",
        "multiple_choice",
        "numeric",
    }
    assert all(question["metadata"]["demo"] is True for question in loaded.questions)
    assert all("mock_response" in question["metadata"] for question in loaded.questions)
    assert loaded.dataset_hash == (
        "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe"
    )


def test_hash_uses_canonical_json_lf_and_final_newline() -> None:
    manifest = _manifest()
    question = _exact()
    expected_payload = (
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
        + json.dumps(
            question,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")

    assert (
        compute_dataset_hash(manifest, [question]) == hashlib.sha256(expected_payload).hexdigest()
    )


def test_hash_is_stable_across_layout_bom_crlf_and_unicode_escape() -> None:
    manifest = _manifest()
    question = _exact()
    question["prompt"] = "Write café."

    compact = load_dataset_bytes(
        _json_bytes(manifest, separators=(",", ":")),
        _jsonl_bytes(question),
    )
    reordered_manifest = {key: manifest[key] for key in reversed(manifest)}
    reordered_question = {key: question[key] for key in reversed(question)}
    pretty_manifest = b"\xef\xbb\xbf" + _json_bytes(reordered_manifest, indent=2)
    escaped_question = json.dumps(reordered_question, ensure_ascii=True).encode("ascii") + b"\r\n"
    reformatted = load_dataset_bytes(pretty_manifest, escaped_question)

    assert compact.dataset_hash == reformatted.dataset_hash


def test_hash_ignores_importer_derived_manifest_fields_but_preserves_question_order() -> None:
    manifest = _manifest(question_count=2)
    first = _exact("first")
    second = _exact("second")
    enriched = {
        **manifest,
        "dataset_hash": "old-value",
        "database_id": 42,
        "imported_at": "2099-01-01T00:00:00Z",
        "absolute_path": "/private/data",
    }

    baseline = compute_dataset_hash(manifest, [first, second])
    assert compute_dataset_hash(enriched, [first, second]) == baseline
    assert compute_dataset_hash(manifest, [second, first]) != baseline


@pytest.mark.parametrize(
    ("mutate", "expected_code", "pointer"),
    [
        (
            lambda value: value.update({"unexpected": True}),
            "additional_field_not_allowed",
            "/unexpected",
        ),
        (
            lambda value: value.update({"schema_version": "v2"}),
            "unsupported_schema_version",
            "/schema_version",
        ),
        (lambda value: value.update({"id": "Not Valid"}), "pattern_mismatch", "/id"),
        (lambda value: value.update({"question_count": True}), "invalid_type", "/question_count"),
        (lambda value: value.update({"evaluator": "exact_match_v1"}), "invalid_type", "/evaluator"),
        (
            lambda value: value["evaluator"]["mapping"].update({"numeric": "exact_match_v1"}),
            "incompatible_evaluator_mapping",
            "/evaluator/mapping/numeric",
        ),
        (
            lambda value: value.update(
                {"prompt_template": {"system": "", "user": "{prompt} {unknown}"}}
            ),
            "unsupported_prompt_placeholder",
            "/prompt_template/user",
        ),
        (
            lambda value: value.update(
                {"prompt_template": {"system": "", "user": "choices: {choices}"}}
            ),
            "required_prompt_placeholder_missing",
            "/prompt_template/user",
        ),
    ],
)
def test_manifest_schema_and_closed_evaluator_mapping(
    mutate: Any,
    expected_code: str,
    pointer: str,
) -> None:
    manifest = _manifest()
    mutate(manifest)

    with pytest.raises(DatasetValidationError) as caught:
        load_dataset_bytes(_json_bytes(manifest), _jsonl_bytes(_exact()))

    issue = next(issue for issue in caught.value.issues if issue.code == expected_code)
    assert issue.file == "manifest.json"
    assert issue.pointer == pointer


def test_jsonl_syntax_error_reports_exact_file_line_and_column() -> None:
    questions = _jsonl_bytes(_exact("good")) + b'{"id":"broken","type":}\n'
    manifest = _manifest(question_count=2)

    with pytest.raises(DatasetValidationError) as caught:
        load_dataset_bytes(_json_bytes(manifest), questions)

    issue = next(issue for issue in caught.value.issues if issue.code == "invalid_json")
    assert issue.file == "questions.jsonl"
    assert issue.line == 2
    assert issue.column == 23
    assert "questions.jsonl:2:23" in str(caught.value)


def test_invalid_utf8_reports_the_physical_jsonl_line() -> None:
    questions = _jsonl_bytes(_exact("good")) + b"{\xff}\n"

    with pytest.raises(DatasetValidationError) as caught:
        load_dataset_bytes(_json_bytes(_manifest(question_count=2)), questions)

    issue = caught.value.issues[0]
    assert issue.file == "questions.jsonl"
    assert issue.code == "invalid_utf8"
    assert issue.line == 2
    assert issue.column == 2

    with pytest.raises(DatasetValidationError) as multibyte:
        load_dataset_bytes(_json_bytes(_manifest()), "中".encode() + b"\xff\n")
    assert multibyte.value.issues[0].column == 2


@pytest.mark.parametrize(
    ("questions", "code", "line", "pointer"),
    [
        (b"\n", "empty_jsonl_line", 1, None),
        (
            b'{"id":"q","id":"again","type":"exact_match","prompt":"p","answer":"a","metadata":{}}\n',
            "duplicate_json_key",
            1,
            None,
        ),
        (
            b'{"id":"q","type":"exact_match","prompt":"p","answer":"a","metadata":{},}\n',
            "invalid_json",
            1,
            None,
        ),
        (
            _jsonl_bytes(
                {
                    "id": "q",
                    "type": "multiple_choice",
                    "prompt": "pick",
                    "choices": {"A": "one", "B": "two"},
                    "answer": "C",
                    "metadata": {},
                }
            ),
            "answer_not_in_choices",
            1,
            "/answer",
        ),
        (
            _jsonl_bytes(
                {
                    "id": "q",
                    "type": "numeric",
                    "prompt": "number",
                    "answer": "NaN",
                    "evaluator_config": {"absolute_tolerance": -0.1},
                    "metadata": {},
                }
            ),
            "invalid_numeric_answer",
            1,
            "/answer",
        ),
    ],
)
def test_jsonl_format_and_cross_field_errors_are_located(
    questions: bytes,
    code: str,
    line: int,
    pointer: str | None,
) -> None:
    with pytest.raises(DatasetValidationError) as caught:
        load_dataset_bytes(_json_bytes(_manifest()), questions)

    issue = next(issue for issue in caught.value.issues if issue.code == code)
    assert issue.file == "questions.jsonl"
    assert issue.line == line
    if pointer is not None:
        assert issue.pointer == pointer


def test_duplicate_ids_and_question_count_are_rejected() -> None:
    duplicate_questions = _jsonl_bytes(_exact("same"), _exact("same"))

    with pytest.raises(DatasetValidationError) as duplicate:
        load_dataset_bytes(_json_bytes(_manifest(question_count=2)), duplicate_questions)
    issue = next(issue for issue in duplicate.value.issues if issue.code == "duplicate_question_id")
    assert issue.line == 2
    assert "line 1" in issue.message

    with pytest.raises(DatasetValidationError) as mismatch:
        load_dataset_bytes(_json_bytes(_manifest(question_count=2)), _jsonl_bytes(_exact()))
    _assert_issue(mismatch.value, "question_count_mismatch", "manifest.json")


def test_non_finite_json_numbers_and_tolerances_are_rejected() -> None:
    invalid_constant = (
        b'{"id":"q","type":"numeric","prompt":"p","answer":NaN,'
        b'"evaluator_config":{},"metadata":{}}\n'
    )
    with pytest.raises(DatasetValidationError) as non_standard:
        load_dataset_bytes(_json_bytes(_manifest()), invalid_constant)
    issue = next(issue for issue in non_standard.value.issues if issue.code == "non_finite_number")
    assert issue.line == 1
    assert issue.column > 1

    overflow = (
        b'{"id":"q","type":"numeric","prompt":"p","answer":1e999,'
        b'"evaluator_config":{"relative_tolerance":0},"metadata":{}}\n'
    )
    with pytest.raises(DatasetValidationError) as overflow_error:
        load_dataset_bytes(_json_bytes(_manifest()), overflow)
    _assert_issue(overflow_error.value, "non_finite_number", "questions.jsonl")

    question = {
        "id": "q",
        "type": "numeric",
        "prompt": "p",
        "answer": 1,
        "evaluator_config": {"relative_tolerance": -0.01},
        "metadata": {},
    }
    with pytest.raises(DatasetValidationError) as negative:
        load_dataset_bytes(_json_bytes(_manifest()), _jsonl_bytes(question))
    _assert_issue(negative.value, "invalid_tolerance", "questions.jsonl")


def test_line_and_file_resource_limits_are_enforced_before_schema_work() -> None:
    manifest_data = _json_bytes(_manifest())
    questions_data = _jsonl_bytes(_exact())

    with pytest.raises(DatasetValidationError) as manifest_error:
        load_dataset_bytes(
            manifest_data,
            questions_data,
            limits=DatasetLimits(max_manifest_bytes=8),
        )
    _assert_issue(manifest_error.value, "file_too_large", "manifest.json")

    with pytest.raises(DatasetValidationError) as line_error:
        load_dataset_bytes(
            manifest_data,
            questions_data,
            limits=DatasetLimits(max_line_bytes=20),
        )
    issue = next(issue for issue in line_error.value.issues if issue.code == "line_too_large")
    assert issue.line == 1


def test_safe_zip_round_trip_matches_directory_hash() -> None:
    manifest_data = (DEMO_DIRECTORY / "manifest.json").read_bytes()
    questions_data = (DEMO_DIRECTORY / "questions.jsonl").read_bytes()
    archive = _zip_bytes([("questions.jsonl", questions_data), ("manifest.json", manifest_data)])

    zipped = load_dataset_zip_bytes(archive)
    direct = load_dataset_directory(DEMO_DIRECTORY)

    assert zipped.dataset_hash == direct.dataset_hash
    assert zipped.questions == direct.questions


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../manifest.json",
        "/manifest.json",
        "C:/manifest.json",
        "folder/manifest.json",
    ],
)
def test_zip_rejects_traversal_absolute_and_nested_paths(unsafe_name: str) -> None:
    archive = _zip_bytes(
        [
            (unsafe_name, _json_bytes(_manifest())),
            ("questions.jsonl", _jsonl_bytes(_exact())),
        ]
    )

    with pytest.raises(DatasetValidationError) as caught:
        load_dataset_zip_bytes(archive)

    assert caught.value.issues[0].code in {
        "unsafe_archive_path",
        "unexpected_archive_entry",
    }


def test_zip_rejects_duplicate_and_extra_entries() -> None:
    manifest_data = _json_bytes(_manifest())
    questions_data = _jsonl_bytes(_exact())
    duplicate = _zip_bytes(
        [
            ("manifest.json", manifest_data),
            ("manifest.json", manifest_data),
            ("questions.jsonl", questions_data),
        ]
    )
    with pytest.raises(DatasetValidationError) as duplicate_error:
        load_dataset_zip_bytes(duplicate)
    _assert_issue(duplicate_error.value, "duplicate_archive_entry", "archive.zip")

    extra = _zip_bytes(
        [
            ("manifest.json", manifest_data),
            ("questions.jsonl", questions_data),
            ("notes.txt", b"not allowed"),
        ]
    )
    with pytest.raises(DatasetValidationError) as extra_error:
        load_dataset_zip_bytes(extra)
    _assert_issue(extra_error.value, "unexpected_archive_entry", "archive.zip")


def test_zip_rejects_high_expansion_ratio_and_archive_byte_limit() -> None:
    bomb = _zip_bytes(
        [
            ("manifest.json", b"A" * 500_000),
            ("questions.jsonl", b"{}\n"),
        ]
    )
    with pytest.raises(DatasetValidationError) as bomb_error:
        load_dataset_zip_bytes(bomb)
    _assert_issue(bomb_error.value, "compression_bomb", "archive.zip")

    ordinary = _zip_bytes(
        [
            ("manifest.json", _json_bytes(_manifest())),
            ("questions.jsonl", _jsonl_bytes(_exact())),
        ],
        compression=zipfile.ZIP_STORED,
    )
    with pytest.raises(DatasetValidationError) as archive_error:
        load_dataset_zip_bytes(ordinary, limits=DatasetLimits(max_archive_bytes=10))
    _assert_issue(archive_error.value, "archive_too_large", "archive.zip")


def test_directory_loader_rejects_symlinked_required_file(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    outside = tmp_path / "outside.json"
    outside.write_bytes(_json_bytes(_manifest()))
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "questions.jsonl").write_bytes(_jsonl_bytes(_exact()))
    try:
        (dataset / "manifest.json").symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks is not permitted")

    with pytest.raises(DatasetValidationError) as caught:
        load_dataset_directory(dataset)

    _assert_issue(caught.value, "unsafe_file", "manifest.json")


def test_validation_does_not_mutate_source_values() -> None:
    manifest = _manifest()
    question = _exact()
    before_manifest = deepcopy(manifest)
    before_question = deepcopy(question)

    loaded = load_dataset_bytes(_json_bytes(manifest), _jsonl_bytes(question))

    assert manifest == before_manifest
    assert question == before_question
    assert "evaluator_config" not in loaded.questions[0]
