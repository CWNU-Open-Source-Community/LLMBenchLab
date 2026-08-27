from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as parquet
import pytest

from app.services.dataset_loader import load_dataset_zip_bytes
from app.standard_datasets import gpqa as gpqa_module
from app.standard_datasets import mmlu_pro as mmlu_module
from app.standard_datasets.common import StandardDatasetError
from app.standard_datasets.gpqa import prepare_gpqa_diamond
from app.standard_datasets.mmlu_pro import prepare_mmlu_pro


def _parquet_bytes(rows: list[dict[str, Any]]) -> bytes:
    output = io.BytesIO()
    parquet.write_table(pa.Table.from_pylist(rows), output)
    return output.getvalue()


def _mmlu_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    test_rows: list[dict[str, Any]] = []
    validation_rows: list[dict[str, Any]] = []
    for category in ("math", "physics"):
        for index in range(3):
            test_rows.append(
                {
                    "question_id": f"{category}-{index}",
                    "question": f"Synthetic {category} test question {index}?",
                    "options": [
                        f"Correct {category} {index}",
                        f"Distractor {category} {index} B",
                        f"Distractor {category} {index} C",
                        f"Distractor {category} {index} D",
                    ],
                    "answer": "A",
                    "answer_index": 0,
                    "cot_content": "",
                    "category": category,
                    "src": "synthetic-fixture",
                }
            )
        for index in range(5):
            validation_rows.append(
                {
                    "question_id": f"validation-{category}-{index}",
                    "question": f"Synthetic {category} validation question {index}?",
                    "options": ["Correct", "Wrong B", "Wrong C", "Wrong D"],
                    "answer": "A",
                    "answer_index": 0,
                    "cot_content": (
                        "A: Let's think step by step. "
                        f"Validation reasoning marker {category}-{index}. The answer is (A)."
                    ),
                    "category": category,
                    "src": "synthetic-fixture",
                }
            )
    return test_rows, validation_rows


def _patch_mmlu_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    test_rows: list[dict[str, Any]] | None = None,
    validation_rows: list[dict[str, Any]] | None = None,
) -> dict[str, bytes]:
    default_test, default_validation = _mmlu_rows()
    test_rows = default_test if test_rows is None else test_rows
    validation_rows = default_validation if validation_rows is None else validation_rows
    test_data = _parquet_bytes(test_rows)
    validation_data = _parquet_bytes(validation_rows)
    test_source = replace(
        mmlu_module.MMLU_PRO_TEST_SOURCE,
        url="https://fixtures.invalid/mmlu-test.parquet",
        sha256=hashlib.sha256(test_data).hexdigest(),
        size_bytes=len(test_data),
        cache_name="mmlu-test-fixture.parquet",
    )
    validation_source = replace(
        mmlu_module.MMLU_PRO_VALIDATION_SOURCE,
        url="https://fixtures.invalid/mmlu-validation.parquet",
        sha256=hashlib.sha256(validation_data).hexdigest(),
        size_bytes=len(validation_data),
        cache_name="mmlu-validation-fixture.parquet",
    )
    monkeypatch.setattr(mmlu_module, "MMLU_PRO_TEST_SOURCE", test_source)
    monkeypatch.setattr(mmlu_module, "MMLU_PRO_VALIDATION_SOURCE", validation_source)
    monkeypatch.setattr(mmlu_module, "MMLU_PRO_TEST_EXPECTED_ROWS", len(test_rows))
    monkeypatch.setattr(mmlu_module, "MMLU_PRO_VALIDATION_EXPECTED_ROWS", len(validation_rows))
    return {test_source.url: test_data, validation_source.url: validation_data}


def _gpqa_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    domains = ("Chemistry", "Physics", "Biology")
    for index in range(198):
        rows.append(
            {
                "Question": f"Synthetic graduate question {index}?",
                "Correct Answer": f"Correct answer {index}",
                "Incorrect Answer 1": f"Distractor {index}-1",
                "Incorrect Answer 2": f"Distractor {index}-2",
                "Incorrect Answer 3": f"Distractor {index}-3",
                "Subdomain": f"Synthetic subdomain {index % 5}",
                "Record ID": f"fixture-{index:03d}",
                "High-level domain": domains[index % len(domains)],
                "Explanation": f"Sensitive explanation {index}",
                "Question Writer": f"Sensitive Person {index}",
            }
        )
    return rows


def _gpqa_csv_bytes(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    fieldnames = list(rows[0])
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _gpqa_archive(csv_data: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(gpqa_module.GPQA_DIAMOND_PATH, csv_data)
        archive.writestr("dataset/license.txt", b"Synthetic fixture only")
    return output.getvalue()


def _patch_gpqa_source(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[dict[str, str]] | None = None,
) -> dict[str, bytes]:
    rows = _gpqa_rows() if rows is None else rows
    csv_data = _gpqa_csv_bytes(rows)
    archive_data = _gpqa_archive(csv_data)
    source = replace(
        gpqa_module.GPQA_SOURCE,
        url="https://fixtures.invalid/gpqa-dataset.zip",
        sha256=hashlib.sha256(archive_data).hexdigest(),
        size_bytes=len(archive_data),
        cache_name="gpqa-fixture.zip",
    )
    monkeypatch.setattr(gpqa_module, "GPQA_SOURCE", source)
    monkeypatch.setattr(gpqa_module, "GPQA_DIAMOND_SHA256", hashlib.sha256(csv_data).hexdigest())
    monkeypatch.setattr(gpqa_module, "GPQA_DIAMOND_EXPECTED_ROWS", len(rows))
    return {source.url: archive_data}


def _fixture_fetcher(sources: dict[str, bytes], calls: list[str]) -> Callable[[str], bytes]:
    def fetch(url: str) -> bytes:
        calls.append(url)
        return sources[url]

    return fetch


def test_mmlu_direct_is_cached_valid_and_byte_reproducible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _patch_mmlu_sources(monkeypatch)
    calls: list[str] = []
    first = prepare_mmlu_pro(
        tmp_path / "first",
        cache_dir=tmp_path / "cache",
        profile="direct",
        fetcher=_fixture_fetcher(sources, calls),
    )

    def unexpected_fetch(_url: str) -> bytes:
        raise AssertionError("verified cache entries should prevent a second fetch")

    second = prepare_mmlu_pro(
        tmp_path / "second",
        cache_dir=tmp_path / "cache",
        profile="direct",
        fetcher=unexpected_fetch,
    )

    assert calls == list(sources)
    assert first.archive_sha256 == second.archive_sha256
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert first.loaded_dataset.dataset_hash == second.loaded_dataset.dataset_hash
    assert first.source_metadata["revision"] == mmlu_module.MMLU_PRO_REVISION
    assert first.source_metadata["profile"] == "direct"
    assert len(first.loaded_dataset.questions) == 6
    assert "Validation reasoning marker" not in first.loaded_dataset.questions[0]["prompt"]

    round_trip = load_dataset_zip_bytes(first.archive_path.read_bytes())
    assert round_trip.dataset_hash == first.loaded_dataset.dataset_hash
    assert round_trip.questions == first.loaded_dataset.questions


def test_mmlu_official_cot_uses_only_five_same_category_examples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _patch_mmlu_sources(monkeypatch)
    result = prepare_mmlu_pro(
        tmp_path / "output",
        cache_dir=tmp_path / "cache",
        profile="official_cot",
        categories=["MATH"],
        limit=1,
        fetcher=_fixture_fetcher(sources, []),
    )

    question = result.loaded_dataset.questions[0]
    prompt = question["prompt"]
    assert result.loaded_dataset.manifest["id"] == "mmlu-pro-official-cot"
    assert question["metadata"]["category"] == "math"
    assert prompt.count("Validation reasoning marker math-") == 5
    assert "Validation reasoning marker physics-" not in prompt
    assert prompt.endswith("Answer: Let's think step by step.")


def test_mmlu_filter_and_limit_change_dataset_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _patch_mmlu_sources(monkeypatch)
    fetcher = _fixture_fetcher(sources, [])
    one = prepare_mmlu_pro(
        tmp_path / "one",
        cache_dir=tmp_path / "cache",
        profile="direct",
        categories=["math"],
        limit=1,
        fetcher=fetcher,
    )
    two = prepare_mmlu_pro(
        tmp_path / "two",
        cache_dir=tmp_path / "cache",
        profile="direct",
        categories=["math"],
        limit=2,
        fetcher=fetcher,
    )

    assert len(one.loaded_dataset.questions) == 1
    assert len(two.loaded_dataset.questions) == 2
    assert one.loaded_dataset.manifest["version"] != two.loaded_dataset.manifest["version"]
    assert one.loaded_dataset.dataset_hash != two.loaded_dataset.dataset_hash


def test_mmlu_rejects_source_sha_mismatch_before_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _patch_mmlu_sources(monkeypatch)
    monkeypatch.setattr(
        mmlu_module,
        "MMLU_PRO_TEST_SOURCE",
        replace(mmlu_module.MMLU_PRO_TEST_SOURCE, sha256="0" * 64),
    )

    with pytest.raises(StandardDatasetError, match="SHA-256 mismatch"):
        prepare_mmlu_pro(
            tmp_path / "output",
            cache_dir=tmp_path / "cache",
            profile="direct",
            fetcher=_fixture_fetcher(sources, []),
        )


def test_mmlu_rejects_inconsistent_answer_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_rows, validation_rows = _mmlu_rows()
    test_rows[0]["answer_index"] = 1
    sources = _patch_mmlu_sources(
        monkeypatch,
        test_rows=test_rows,
        validation_rows=validation_rows,
    )

    with pytest.raises(StandardDatasetError, match="inconsistent answer"):
        prepare_mmlu_pro(
            tmp_path / "output",
            cache_dir=tmp_path / "cache",
            profile="direct",
            fetcher=_fixture_fetcher(sources, []),
        )


def test_gpqa_shuffle_is_record_local_seeded_and_drops_sensitive_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _gpqa_rows()
    sources = _patch_gpqa_source(monkeypatch, rows=rows)
    first = prepare_gpqa_diamond(
        tmp_path / "first",
        cache_dir=tmp_path / "cache",
        seed=123,
        fetcher=_fixture_fetcher(sources, []),
    )

    choices_by_id = {
        question["id"]: question["choices"] for question in first.loaded_dataset.questions
    }
    reordered_sources = _patch_gpqa_source(monkeypatch, rows=list(reversed(rows)))
    reordered = prepare_gpqa_diamond(
        tmp_path / "reordered",
        cache_dir=tmp_path / "reordered-cache",
        seed=123,
        fetcher=_fixture_fetcher(reordered_sources, []),
    )
    reordered_choices = {
        question["id"]: question["choices"] for question in reordered.loaded_dataset.questions
    }

    assert choices_by_id == reordered_choices
    assert {question["answer"] for question in first.loaded_dataset.questions} != {"A"}
    for index, question in enumerate(first.loaded_dataset.questions):
        assert question["choices"][question["answer"]] == f"Correct answer {index}"
        assert set(question["metadata"]) == {"domain", "subdomain", "shuffle_seed"}
    generated = first.archive_path.read_bytes()
    assert b"Sensitive Person" not in generated
    assert b"Sensitive explanation" not in generated
    assert (
        first.loaded_dataset.manifest["prompt_template"]["user"]
        .splitlines()[0]
        .startswith("Answer the following multiple choice question")
    )
    assert '"Answer: X"' in first.loaded_dataset.manifest["prompt_template"]["user"]


def test_gpqa_seed_filter_and_limit_are_reproducible_and_identified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _patch_gpqa_source(monkeypatch)
    fetcher = _fixture_fetcher(sources, [])
    first = prepare_gpqa_diamond(
        tmp_path / "first",
        cache_dir=tmp_path / "cache",
        seed=7,
        domains=["chemistry"],
        limit=3,
        fetcher=fetcher,
    )
    same = prepare_gpqa_diamond(
        tmp_path / "same",
        cache_dir=tmp_path / "cache",
        seed=7,
        domains=["Chemistry"],
        limit=3,
        fetcher=fetcher,
    )
    different_seed = prepare_gpqa_diamond(
        tmp_path / "different",
        cache_dir=tmp_path / "cache",
        seed=8,
        domains=["Chemistry"],
        limit=3,
        fetcher=fetcher,
    )

    assert first.archive_sha256 == same.archive_sha256
    assert first.loaded_dataset.dataset_hash == same.loaded_dataset.dataset_hash
    assert len(first.loaded_dataset.questions) == 3
    assert all(
        question["metadata"]["domain"] == "Chemistry" for question in first.loaded_dataset.questions
    )
    assert (
        first.loaded_dataset.manifest["version"]
        != different_seed.loaded_dataset.manifest["version"]
    )
    assert first.loaded_dataset.dataset_hash != different_seed.loaded_dataset.dataset_hash
    assert (
        first.loaded_dataset.questions[0]["choices"]
        != different_seed.loaded_dataset.questions[0]["choices"]
    )


def test_gpqa_rejects_bad_required_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _gpqa_rows()
    rows[0]["Correct Answer"] = ""
    sources = _patch_gpqa_source(monkeypatch, rows=rows)

    with pytest.raises(StandardDatasetError, match="invalid Correct Answer"):
        prepare_gpqa_diamond(
            tmp_path / "output",
            cache_dir=tmp_path / "cache",
            fetcher=_fixture_fetcher(sources, []),
        )
