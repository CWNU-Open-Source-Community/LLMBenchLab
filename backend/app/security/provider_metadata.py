"""Fail-closed normalization for optional Provider transport metadata.

These values are useful correlation evidence, but none is required for scoring.
Only short opaque tokens are safe to persist or expose.  Anything resembling a
credential, containing whitespace/control characters, or exceeding its column
boundary is therefore represented as ``None`` instead of being reflected.
"""

from __future__ import annotations

import re

_SAFE_PROVIDER_METADATA_PATTERN = re.compile(r"[A-Za-z0-9._:/@+=-]+")
_COMMON_SECRET_PATTERN = re.compile(
    r"(?i)(?:"
    r"\b(?:proxy[-_ ]?)?authorization\b|"
    r"\bbearer\b|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|cookie)\b|"
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,})\b"
    r")"
)
_MAX_HTTP_ATTEMPT_COUNT = 2_147_483_647


def normalize_provider_metadata(value: object, *, max_length: int) -> str | None:
    """Return one bounded opaque token, or ``None`` for unsafe evidence."""

    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= max_length
        or _SAFE_PROVIDER_METADATA_PATTERN.fullmatch(value) is None
        or _COMMON_SECRET_PATTERN.search(value) is not None
    ):
        return None
    return value


def normalize_http_attempt_count(value: object) -> int | None:
    """Return a positive database-safe HTTP attempt count, else ``None``."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_HTTP_ATTEMPT_COUNT
    ):
        return None
    return value
