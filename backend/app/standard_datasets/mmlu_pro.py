"""Pinned MMLU-Pro test-set conversion for LLMBenchLab dataset v1."""

from __future__ import annotations

import io
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

import pyarrow.parquet as parquet

from app.standard_datasets.common import (
    Fetcher,
    PreparedDataset,
    SourceFile,
    StandardDatasetError,
    build_manifest,
    load_cached_source,
    normalize_selection,
    source_file_metadata,
    transformation_fingerprint,
    validate_limit,
    write_dataset_archive,
)

MMLU_PRO_REVISION = "b189ec765aa7ed75c8acfea42df31fdae71f97be"
MMLU_PRO_CONVERTER_VERSION = "1"
MMLU_PRO_COT_SHOTS = 5
MMLU_PRO_TEST_EXPECTED_ROWS = 12_032
MMLU_PRO_VALIDATION_EXPECTED_ROWS = 70

MMLU_PRO_TEST_SOURCE = SourceFile(
    name="MMLU-Pro test Parquet",
    url=(
        "https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro/resolve/"
        f"{MMLU_PRO_REVISION}/data/test-00000-of-00001.parquet?download=true"
    ),
    sha256="0e24a191921c2f453518a537a8b2117bd137e7714d4ef1565e9ba06c1ecb9ad8",
    size_bytes=4_144_185,
    cache_name=f"mmlu-pro-{MMLU_PRO_REVISION}-test.parquet",
)
MMLU_PRO_VALIDATION_SOURCE = SourceFile(
    name="MMLU-Pro validation Parquet",
    url=(
        "https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro/resolve/"
        f"{MMLU_PRO_REVISION}/data/validation-00000-of-00001.parquet?download=true"
    ),
    sha256="139423c23722e480c807ac4a191409a710cfce4eba744c1d641cf88e730e2078",
    size_bytes=42_857,
    cache_name=f"mmlu-pro-{MMLU_PRO_REVISION}-validation.parquet",
)

MMLUProfile = Literal["direct", "official_cot"]
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_REQUIRED_COLUMNS = frozenset(
    {
        "question_id",
        "question",
        "options",
        "answer",
        "answer_index",
        "cot_content",
        "category",
        "src",
    }
)
_OFFICIAL_INITIAL_PROMPT = (
    "The following are multiple choice questions (with answers) about {category}. "
    'Think step by step and then finish your answer with "the answer is (X)" where X is '
    "the correct letter choice.\n"
)


def _read_parquet(data: bytes, *, source_name: str, expected_rows: int) -> list[dict[str, Any]]:
    try:
        table = parquet.read_table(io.BytesIO(data))
        rows = table.to_pylist()
    except Exception as exc:
        raise StandardDatasetError(f"{source_name} is not a readable Parquet file") from exc
    missing = _REQUIRED_COLUMNS - set(table.column_names)
    if missing:
        raise StandardDatasetError(
            f"{source_name} is missing required columns: {', '.join(sorted(missing))}"
        )
    if len(rows) != expected_rows:
        raise StandardDatasetError(
            f"{source_name} row-count mismatch: expected {expected_rows}, received {len(rows)}"
        )
    return rows


def _require_text(row: Mapping[str, Any], field: str, *, row_number: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise StandardDatasetError(f"MMLU-Pro row {row_number} has invalid {field}")
    return value.strip()


def _normalize_row(row: Mapping[str, Any], *, row_number: int) -> dict[str, Any]:
    question_id = row.get("question_id")
    if isinstance(question_id, bool) or not isinstance(question_id, (int, str)):
        raise StandardDatasetError(f"MMLU-Pro row {row_number} has invalid question_id")
    normalized_id = str(question_id).strip()
    if not normalized_id:
        raise StandardDatasetError(f"MMLU-Pro row {row_number} has empty question_id")

    raw_options = row.get("options")
    if not isinstance(raw_options, list):
        raise StandardDatasetError(f"MMLU-Pro row {row_number} has invalid options")
    options: list[str] = []
    for option in raw_options:
        if option == "N/A":
            continue
        if not isinstance(option, str) or not option.strip():
            raise StandardDatasetError(f"MMLU-Pro row {row_number} has an invalid option")
        options.append(option.strip())
    if not 2 <= len(options) <= 26:
        raise StandardDatasetError(f"MMLU-Pro row {row_number} has an unsupported option count")

    answer = _require_text(row, "answer", row_number=row_number).upper()
    answer_index = row.get("answer_index")
    if isinstance(answer_index, bool) or not isinstance(answer_index, int):
        raise StandardDatasetError(f"MMLU-Pro row {row_number} has invalid answer_index")
    if not 0 <= answer_index < len(options) or _LETTERS[answer_index] != answer:
        raise StandardDatasetError(
            f"MMLU-Pro row {row_number} has inconsistent answer and answer_index"
        )

    source = row.get("src")
    if source is None:
        source = "unknown"
    if not isinstance(source, str):
        raise StandardDatasetError(f"MMLU-Pro row {row_number} has invalid src")
    cot_content = row.get("cot_content")
    if cot_content is None:
        cot_content = ""
    if not isinstance(cot_content, str):
        raise StandardDatasetError(f"MMLU-Pro row {row_number} has invalid cot_content")

    return {
        "question_id": normalized_id,
        "question": _require_text(row, "question", row_number=row_number),
        "options": options,
        "answer": answer,
        "answer_index": answer_index,
        "category": _require_text(row, "category", row_number=row_number),
        "src": source.strip() or "unknown",
        "cot_content": cot_content.strip(),
    }


def _normalize_rows(rows: list[dict[str, Any]], *, split: str) -> list[dict[str, Any]]:
    normalized = [_normalize_row(row, row_number=index) for index, row in enumerate(rows, start=1)]
    seen: set[str] = set()
    for row in normalized:
        question_id = row["question_id"]
        if question_id in seen:
            raise StandardDatasetError(f"MMLU-Pro {split} contains duplicate question_id")
        seen.add(question_id)
    return normalized


def _format_question(row: Mapping[str, Any]) -> str:
    choices = "".join(
        f"{_LETTERS[index]}. {option}\n" for index, option in enumerate(row["options"])
    )
    return f"Question:\n{row['question']}\nOptions:\n{choices}"


def _format_official_example(row: Mapping[str, Any]) -> str:
    cot_content = row["cot_content"]
    if not cot_content:
        raise StandardDatasetError(
            f"MMLU-Pro validation example {row['question_id']} has empty cot_content"
        )
    cot_content = cot_content.replace(
        "A: Let's think step by step.",
        "Answer: Let's think step by step.",
    )
    return f"{_format_question(row)}{cot_content}\n\n"


def _official_prompt(
    row: Mapping[str, Any],
    examples_by_category: Mapping[str, list[dict[str, Any]]],
) -> str:
    category = row["category"]
    examples = examples_by_category.get(category, [])
    if len(examples) < MMLU_PRO_COT_SHOTS:
        raise StandardDatasetError(
            f"MMLU-Pro category {category!r} has fewer than {MMLU_PRO_COT_SHOTS} "
            "validation examples"
        )
    prefix = _OFFICIAL_INITIAL_PROMPT.format(category=category)
    few_shot = "".join(
        _format_official_example(example) for example in examples[:MMLU_PRO_COT_SHOTS]
    )
    return f"{prefix}{few_shot}{_format_question(row)}Answer: Let's think step by step."


def _direct_prompt(row: Mapping[str, Any]) -> str:
    return (
        "Answer the following multiple choice question. Respond with only the correct option "
        f"letter.\n\n{_format_question(row)}Answer:"
    )


def _source_description(
    *,
    profile: MMLUProfile,
    categories: tuple[str, ...] | None,
    limit: int | None,
) -> str:
    selected = ",".join(categories) if categories is not None else "all"
    return (
        f"TIGER-Lab/MMLU-Pro@{MMLU_PRO_REVISION}; "
        f"test_sha256={MMLU_PRO_TEST_SOURCE.sha256}; "
        f"validation_sha256={MMLU_PRO_VALIDATION_SOURCE.sha256}; "
        f"profile={profile}; categories={selected}; limit={limit or 'all'}; "
        f"converter={MMLU_PRO_CONVERTER_VERSION}"
    )


def prepare_mmlu_pro(
    output_dir: str | Path,
    *,
    cache_dir: str | Path,
    profile: MMLUProfile,
    categories: Iterable[str] | None = None,
    limit: int | None = None,
    fetcher: Fetcher | None = None,
) -> PreparedDataset:
    """Download, verify, transform, cache, and archive pinned MMLU-Pro."""

    if profile not in {"direct", "official_cot"}:
        raise StandardDatasetError("profile must be 'direct' or 'official_cot'")
    limit = validate_limit(limit)
    test_data = load_cached_source(MMLU_PRO_TEST_SOURCE, cache_dir, fetcher=fetcher)
    validation_data = load_cached_source(MMLU_PRO_VALIDATION_SOURCE, cache_dir, fetcher=fetcher)
    test_rows = _normalize_rows(
        _read_parquet(
            test_data,
            source_name=MMLU_PRO_TEST_SOURCE.name,
            expected_rows=MMLU_PRO_TEST_EXPECTED_ROWS,
        ),
        split="test",
    )
    validation_rows = _normalize_rows(
        _read_parquet(
            validation_data,
            source_name=MMLU_PRO_VALIDATION_SOURCE.name,
            expected_rows=MMLU_PRO_VALIDATION_EXPECTED_ROWS,
        ),
        split="validation",
    )

    available_categories = {row["category"] for row in test_rows}
    selected_categories = normalize_selection(
        categories,
        available_categories,
        label="category",
    )
    selected_set = set(selected_categories) if selected_categories is not None else None
    selected_rows = [
        row for row in test_rows if selected_set is None or row["category"] in selected_set
    ]
    if limit is not None:
        selected_rows = selected_rows[:limit]
    if not selected_rows:
        raise StandardDatasetError("MMLU-Pro selection produced no test questions")

    examples_by_category: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in validation_rows:
        examples_by_category[row["category"]].append(row)

    questions: list[dict[str, Any]] = []
    for row in selected_rows:
        choices = {_LETTERS[index]: option for index, option in enumerate(row["options"])}
        prompt = (
            _official_prompt(row, examples_by_category)
            if profile == "official_cot"
            else _direct_prompt(row)
        )
        questions.append(
            {
                "id": f"mmlu-pro-{row['question_id']}",
                "type": "multiple_choice",
                "prompt": prompt,
                "choices": choices,
                "answer": row["answer"],
                "metadata": {
                    "category": row["category"],
                    "profile": profile,
                    "source": row["src"],
                },
            }
        )

    transform = {
        "converter_version": MMLU_PRO_CONVERTER_VERSION,
        "dataset_revision": MMLU_PRO_REVISION,
        "test_sha256": MMLU_PRO_TEST_SOURCE.sha256,
        "validation_sha256": MMLU_PRO_VALIDATION_SOURCE.sha256,
        "profile": profile,
        "categories": selected_categories or "all",
        "limit": limit,
    }
    fingerprint = transformation_fingerprint(transform)
    dataset_id = f"mmlu-pro-{profile.replace('_', '-')}"
    version = f"1.0.0-{fingerprint}"
    description = (
        "Pinned MMLU-Pro test split using the official five-shot category CoT prompt."
        if profile == "official_cot"
        else "Pinned MMLU-Pro test split using a lower-cost direct-answer profile; not directly "
        "comparable with the official CoT leaderboard."
    )
    manifest = build_manifest(
        dataset_id=dataset_id,
        name=f"MMLU-Pro ({profile})",
        version=version,
        description=description,
        dimension="knowledge_reasoning",
        license_name="MIT",
        source=_source_description(
            profile=profile,
            categories=selected_categories,
            limit=limit,
        ),
        prompt_template={"system": "", "user": "{prompt}"},
        question_count=len(questions),
    )
    archive_path, loaded, archive_sha256 = write_dataset_archive(
        output_dir,
        manifest,
        questions,
    )
    source_metadata = {
        "dataset": "MMLU-Pro",
        "revision": MMLU_PRO_REVISION,
        "profile": profile,
        "categories": list(selected_categories) if selected_categories is not None else "all",
        "limit": limit,
        "converter_version": MMLU_PRO_CONVERTER_VERSION,
        "source_files": [
            source_file_metadata(MMLU_PRO_TEST_SOURCE),
            source_file_metadata(MMLU_PRO_VALIDATION_SOURCE),
        ],
        "dataset_hash": loaded.dataset_hash,
    }
    return PreparedDataset(
        archive_path=archive_path,
        loaded_dataset=loaded,
        archive_sha256=archive_sha256,
        source_metadata=source_metadata,
    )
