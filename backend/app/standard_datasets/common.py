"""Shared, deterministic primitives for trusted standard-dataset converters."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.dataset_loader import LoadedDataset, load_dataset_bytes

Fetcher = Callable[[str], bytes]


class StandardDatasetError(ValueError):
    """Raised when a pinned source cannot be safely converted."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One immutable upstream file in a standard-dataset release."""

    name: str
    url: str
    sha256: str
    size_bytes: int
    cache_name: str

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("standard dataset source URLs must use HTTPS")
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256):
            raise ValueError("source SHA-256 must be 64 lowercase hexadecimal characters")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ValueError("source size must be an integer")
        if self.size_bytes <= 0:
            raise ValueError("source size must be positive")
        if Path(self.cache_name).name != self.cache_name or not self.cache_name:
            raise ValueError("cache_name must be a single filename")


@dataclass(frozen=True, slots=True)
class PreparedDataset:
    """A validated dataset-v1 value and its byte-reproducible local ZIP."""

    archive_path: Path
    loaded_dataset: LoadedDataset
    archive_sha256: str
    source_metadata: Mapping[str, Any]


_EVALUATOR = {
    "name": "builtin-objective",
    "version": "1.0",
    "mapping": {
        "exact_match": "exact_match_v1",
        "multiple_choice": "multiple_choice_v1",
        "numeric": "numeric_v1",
    },
}
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def build_manifest(
    *,
    dataset_id: str,
    name: str,
    version: str,
    description: str,
    dimension: str,
    license_name: str,
    source: str,
    prompt_template: Mapping[str, str],
    question_count: int,
) -> dict[str, Any]:
    """Build the closed dataset-v1 manifest shared by the converters."""

    return {
        "schema_version": "llmbenchlab-dataset-v1",
        "id": dataset_id,
        "name": name,
        "version": version,
        "description": description,
        "dimension": dimension,
        "language": "en",
        "license": license_name,
        "source": source,
        "evaluator": _EVALUATOR,
        "prompt_template": dict(prompt_template),
        "question_count": question_count,
    }


def transformation_fingerprint(value: Mapping[str, Any]) -> str:
    """Return a short stable identity for source and transformation settings."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def normalize_selection(
    requested: Iterable[str] | None,
    available: Iterable[str],
    *,
    label: str,
) -> tuple[str, ...] | None:
    """Normalize a case-insensitive set filter to stable source spellings."""

    if requested is None:
        return None
    lookup = {item.casefold(): item for item in available}
    normalized: set[str] = set()
    for raw_value in requested:
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise StandardDatasetError(f"{label} filters must be non-empty strings")
        value = lookup.get(raw_value.strip().casefold())
        if value is None:
            choices = ", ".join(sorted(lookup.values(), key=str.casefold))
            raise StandardDatasetError(
                f"unknown {label} {raw_value!r}; available values: {choices}"
            )
        normalized.add(value)
    if not normalized:
        raise StandardDatasetError(f"at least one {label} must be selected")
    return tuple(sorted(normalized, key=str.casefold))


def validate_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise StandardDatasetError("limit must be a positive integer")
    return limit


def source_file_metadata(source: SourceFile) -> dict[str, Any]:
    return {
        "name": source.name,
        "url": source.url,
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
    }


def _verify_source(data: bytes, source: SourceFile) -> None:
    if len(data) != source.size_bytes:
        raise StandardDatasetError(
            f"{source.name} size mismatch: expected {source.size_bytes}, received {len(data)}"
        )
    actual = hashlib.sha256(data).hexdigest()
    if actual != source.sha256:
        raise StandardDatasetError(
            f"{source.name} SHA-256 mismatch: expected {source.sha256}, received {actual}"
        )


def _download_https(source: SourceFile) -> bytes:
    request = urllib.request.Request(
        source.url,
        headers={"User-Agent": "LLMBenchLab/0.1 standard-dataset-fetcher"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            final_url = urllib.parse.urlsplit(response.geturl())
            if final_url.scheme != "https":
                raise StandardDatasetError("standard dataset download redirected away from HTTPS")
            data = response.read(source.size_bytes + 1)
    except StandardDatasetError:
        raise
    except (OSError, ValueError) as exc:
        raise StandardDatasetError(f"failed to download pinned source {source.name}") from exc
    return data


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_cached_source(
    source: SourceFile,
    cache_dir: str | os.PathLike[str],
    *,
    fetcher: Fetcher | None = None,
) -> bytes:
    """Return verified source bytes, populating a content-checked local cache."""

    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / source.cache_name
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise StandardDatasetError(f"cache entry for {source.name} must be a regular file")
        try:
            size = target.stat().st_size
            if size != source.size_bytes:
                raise StandardDatasetError(
                    f"cached {source.name} size mismatch: expected {source.size_bytes}, "
                    f"received {size}"
                )
            data = target.read_bytes()
        except OSError as exc:
            raise StandardDatasetError(f"failed to read cached source {source.name}") from exc
        _verify_source(data, source)
        return data

    try:
        data = _download_https(source) if fetcher is None else fetcher(source.url)
    except StandardDatasetError:
        raise
    except Exception as exc:
        raise StandardDatasetError(f"failed to download pinned source {source.name}") from exc
    if not isinstance(data, bytes):
        raise StandardDatasetError(f"fetcher for {source.name} must return bytes")
    _verify_source(data, source)
    _atomic_write(target, data)
    return data


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _zip_info(filename: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=filename, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def write_dataset_archive(
    output_dir: str | os.PathLike[str],
    manifest: Mapping[str, Any],
    questions: Sequence[Mapping[str, Any]],
) -> tuple[Path, LoadedDataset, str]:
    """Validate source values and atomically write a deterministic dataset ZIP."""

    manifest_data = _json_bytes(manifest)
    questions_data = b"".join(_json_bytes(question) for question in questions)
    loaded = load_dataset_bytes(manifest_data, questions_data)

    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr(_zip_info("manifest.json"), manifest_data)
        zf.writestr(_zip_info("questions.jsonl"), questions_data)
    archive_data = output.getvalue()

    output_root = Path(output_dir)
    archive_path = output_root / f"{manifest['id']}-{manifest['version']}.zip"
    if archive_path.exists() and (archive_path.is_symlink() or not archive_path.is_file()):
        raise StandardDatasetError("dataset archive target must be a regular file")
    _atomic_write(archive_path, archive_data)
    return archive_path, loaded, hashlib.sha256(archive_data).hexdigest()
