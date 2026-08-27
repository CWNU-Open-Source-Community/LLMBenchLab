"""Pinned GPQA-Diamond conversion with record-local deterministic shuffling."""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

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

GPQA_REVISION = "56686c06f5e19865c153de0fdb11be3890014df7"
GPQA_CONVERTER_VERSION = "1"
GPQA_DEFAULT_SEED = 42
GPQA_SHUFFLE_ALGORITHM = "sha256-sort-v1"
GPQA_PROMPT_PROFILE = "zero-shot-cot-answer-line-v1"
GPQA_DIAMOND_EXPECTED_ROWS = 198
GPQA_ARCHIVE_PASSWORD = b"deserted-untie-orchid"
GPQA_DIAMOND_PATH = "dataset/gpqa_diamond.csv"
GPQA_DIAMOND_SHA256 = "41d1213cd7a4998605a26c2798500652572007161b3a92817ba46b35befcd305"

GPQA_SOURCE = SourceFile(
    name="GPQA official encrypted archive",
    url=f"https://raw.githubusercontent.com/idavidrein/gpqa/{GPQA_REVISION}/dataset.zip",
    sha256="461ae7329f15a3e35f8184d2dac24b990f34fdf12f366ca4062d8e6638cd08dc",
    size_bytes=2_348_038,
    cache_name=f"gpqa-{GPQA_REVISION}-dataset.zip",
)

_REQUIRED_COLUMNS = frozenset(
    {
        "Question",
        "Correct Answer",
        "Incorrect Answer 1",
        "Incorrect Answer 2",
        "Incorrect Answer 3",
        "Subdomain",
        "Record ID",
        "High-level domain",
    }
)
_LETTERS = "ABCD"


def _read_diamond_csv(archive_data: bytes) -> list[dict[str, str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_data), mode="r") as archive:
            matches = [info for info in archive.infolist() if info.filename == GPQA_DIAMOND_PATH]
            if len(matches) != 1:
                raise StandardDatasetError(
                    f"GPQA archive must contain exactly one {GPQA_DIAMOND_PATH}"
                )
            csv_data = archive.read(matches[0], pwd=GPQA_ARCHIVE_PASSWORD)
    except StandardDatasetError:
        raise
    except (KeyError, OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        raise StandardDatasetError("GPQA archive or password is invalid") from exc

    actual_sha256 = hashlib.sha256(csv_data).hexdigest()
    if actual_sha256 != GPQA_DIAMOND_SHA256:
        raise StandardDatasetError(
            "GPQA Diamond CSV SHA-256 mismatch: "
            f"expected {GPQA_DIAMOND_SHA256}, received {actual_sha256}"
        )
    try:
        text = csv_data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StandardDatasetError("GPQA Diamond CSV is not valid UTF-8") from exc
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fieldnames = set(reader.fieldnames or ())
        missing = _REQUIRED_COLUMNS - fieldnames
        if missing:
            raise StandardDatasetError(
                f"GPQA Diamond CSV is missing required columns: {', '.join(sorted(missing))}"
            )
        rows = list(reader)
    except csv.Error as exc:
        raise StandardDatasetError("GPQA Diamond CSV is malformed") from exc
    if len(rows) != GPQA_DIAMOND_EXPECTED_ROWS:
        raise StandardDatasetError(
            "GPQA Diamond row-count mismatch: "
            f"expected {GPQA_DIAMOND_EXPECTED_ROWS}, received {len(rows)}"
        )
    return rows


def _required_text(row: Mapping[str, Any], field: str, *, row_number: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise StandardDatasetError(f"GPQA Diamond row {row_number} has invalid {field}")
    return value.strip()


def _normalize_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=1):
        record_id = _required_text(row, "Record ID", row_number=row_number)
        if record_id in seen:
            raise StandardDatasetError("GPQA Diamond contains duplicate Record ID values")
        seen.add(record_id)
        normalized.append(
            {
                "record_id": record_id,
                "question": _required_text(row, "Question", row_number=row_number),
                "correct_answer": _required_text(row, "Correct Answer", row_number=row_number),
                "incorrect_answers": [
                    _required_text(row, f"Incorrect Answer {index}", row_number=row_number)
                    for index in range(1, 4)
                ],
                "domain": _required_text(row, "High-level domain", row_number=row_number),
                "subdomain": _required_text(row, "Subdomain", row_number=row_number),
            }
        )
    return normalized


def _shuffled_choices(row: Mapping[str, Any], *, seed: int) -> tuple[dict[str, str], str]:
    options = [(row["correct_answer"], True)]
    options.extend((answer, False) for answer in row["incorrect_answers"])
    options = [
        option
        for _, option in sorted(
            enumerate(options),
            key=lambda indexed: (
                hashlib.sha256(f"{seed}\0{row['record_id']}\0{indexed[0]}".encode()).digest(),
                indexed[0],
            ),
        )
    ]
    choices = {letter: option for letter, (option, _) in zip(_LETTERS, options, strict=True)}
    answer = next(
        letter for letter, (_, is_correct) in zip(_LETTERS, options, strict=True) if is_correct
    )
    return choices, answer


def _validate_seed(seed: int) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise StandardDatasetError("seed must be an integer")
    if not -(2**63) <= seed < 2**63:
        raise StandardDatasetError("seed must fit in a signed 64-bit integer")
    return seed


def _source_description(
    *,
    seed: int,
    domains: tuple[str, ...] | None,
    limit: int | None,
) -> str:
    selected = ",".join(domains) if domains is not None else "all"
    return (
        f"idavidrein/gpqa@{GPQA_REVISION}; archive_sha256={GPQA_SOURCE.sha256}; "
        f"diamond_sha256={GPQA_DIAMOND_SHA256}; seed={seed}; domains={selected}; "
        f"limit={limit or 'all'}; shuffle={GPQA_SHUFFLE_ALGORITHM}; "
        f"prompt={GPQA_PROMPT_PROFILE}; converter={GPQA_CONVERTER_VERSION}"
    )


def prepare_gpqa_diamond(
    output_dir: str | Path,
    *,
    cache_dir: str | Path,
    seed: int = GPQA_DEFAULT_SEED,
    domains: Iterable[str] | None = None,
    limit: int | None = None,
    fetcher: Fetcher | None = None,
) -> PreparedDataset:
    """Download, verify, transform, cache, and archive pinned GPQA-Diamond."""

    seed = _validate_seed(seed)
    limit = validate_limit(limit)
    archive_data = load_cached_source(GPQA_SOURCE, cache_dir, fetcher=fetcher)
    rows = _normalize_rows(_read_diamond_csv(archive_data))

    available_domains = {row["domain"] for row in rows}
    selected_domains = normalize_selection(domains, available_domains, label="domain")
    selected_set = set(selected_domains) if selected_domains is not None else None
    selected_rows = [row for row in rows if selected_set is None or row["domain"] in selected_set]
    if limit is not None:
        selected_rows = selected_rows[:limit]
    if not selected_rows:
        raise StandardDatasetError("GPQA-Diamond selection produced no questions")

    questions: list[dict[str, Any]] = []
    for row in selected_rows:
        choices, answer = _shuffled_choices(row, seed=seed)
        questions.append(
            {
                "id": f"gpqa-{row['record_id']}",
                "type": "multiple_choice",
                "prompt": row["question"],
                "choices": choices,
                "answer": answer,
                "metadata": {
                    "domain": row["domain"],
                    "subdomain": row["subdomain"],
                    "shuffle_seed": seed,
                },
            }
        )

    transform = {
        "converter_version": GPQA_CONVERTER_VERSION,
        "dataset_revision": GPQA_REVISION,
        "archive_sha256": GPQA_SOURCE.sha256,
        "diamond_sha256": GPQA_DIAMOND_SHA256,
        "seed": seed,
        "shuffle_algorithm": GPQA_SHUFFLE_ALGORITHM,
        "prompt_profile": GPQA_PROMPT_PROFILE,
        "domains": selected_domains or "all",
        "limit": limit,
    }
    fingerprint = transformation_fingerprint(transform)
    manifest = build_manifest(
        dataset_id="gpqa-diamond",
        name="GPQA-Diamond",
        version=f"1.0.0-{fingerprint}",
        description=(
            "Pinned GPQA-Diamond multiple-choice benchmark with deterministic, record-local "
            "option shuffling."
        ),
        dimension="graduate_reasoning",
        license_name="CC BY 4.0",
        source=_source_description(seed=seed, domains=selected_domains, limit=limit),
        prompt_template={
            "system": "",
            "user": (
                "Answer the following multiple choice question. Think step by step before "
                'answering. End the last line with "Answer: X", where X is the correct option '
                "letter.\n\n{prompt}\n\nOptions:\n{choices}"
            ),
        },
        question_count=len(questions),
    )
    archive_path, loaded, archive_sha256 = write_dataset_archive(
        output_dir,
        manifest,
        questions,
    )
    source_metadata = {
        "dataset": "GPQA-Diamond",
        "revision": GPQA_REVISION,
        "seed": seed,
        "shuffle_algorithm": GPQA_SHUFFLE_ALGORITHM,
        "prompt_profile": GPQA_PROMPT_PROFILE,
        "domains": list(selected_domains) if selected_domains is not None else "all",
        "limit": limit,
        "converter_version": GPQA_CONVERTER_VERSION,
        "source_files": [
            source_file_metadata(GPQA_SOURCE),
            {
                "name": "GPQA-Diamond CSV inside encrypted archive",
                "path": GPQA_DIAMOND_PATH,
                "sha256": GPQA_DIAMOND_SHA256,
            },
        ],
        "dataset_hash": loaded.dataset_hash,
    }
    return PreparedDataset(
        archive_path=archive_path,
        loaded_dataset=loaded,
        archive_sha256=archive_sha256,
        source_metadata=source_metadata,
    )
