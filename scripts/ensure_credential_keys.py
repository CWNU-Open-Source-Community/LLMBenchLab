#!/usr/bin/env python3
"""Create one local credential-encryption keyring without printing key material."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Final

KEYRING_MAX_BYTES: Final = 64 * 1024
KEYRING_MAX_KEYS: Final = 32
_KEY_BYTES: Final = 32
_KEY_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BASE64URL_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43}=?$")
_OPEN_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_OPEN_CLOEXEC: Final = getattr(os, "O_CLOEXEC", 0)
_OPEN_DIRECTORY: Final = getattr(os, "O_DIRECTORY", 0)
_OPEN_NONBLOCK: Final = getattr(os, "O_NONBLOCK", 0)


class KeyringInitializationError(RuntimeError):
    """A stable failure that never includes file contents or key material."""


class _DuplicateJSONKey(ValueError):
    """Private strict-JSON parsing signal."""


class _DestinationAppeared(RuntimeError):
    """Private signal for a concurrent initializer winning the install race."""


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


def _decode_key(value: object) -> bytes:
    if not isinstance(value, str) or not _BASE64URL_KEY_RE.fullmatch(value):
        raise KeyringInitializationError("Existing credential keyring is invalid.")
    unpadded = value.rstrip("=")
    padded = unpadded + "=" * (-len(unpadded) % 4)
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise KeyringInitializationError("Existing credential keyring is invalid.") from None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if len(decoded) != _KEY_BYTES or canonical != unpadded:
        raise KeyringInitializationError("Existing credential keyring is invalid.")
    return decoded


def _validate_payload(payload: bytes) -> None:
    if not payload or len(payload) > KEYRING_MAX_BYTES:
        raise KeyringInitializationError("Existing credential keyring is invalid.")
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJSONKey):
        raise KeyringInitializationError("Existing credential keyring is invalid.") from None
    if not isinstance(document, dict) or set(document) != {"active_key_id", "keys"}:
        raise KeyringInitializationError("Existing credential keyring is invalid.")

    active_key_id = document["active_key_id"]
    encoded_keys = document["keys"]
    if not isinstance(active_key_id, str) or not _KEY_ID_RE.fullmatch(active_key_id):
        raise KeyringInitializationError("Existing credential keyring is invalid.")
    if (
        not isinstance(encoded_keys, dict)
        or not encoded_keys
        or len(encoded_keys) > KEYRING_MAX_KEYS
    ):
        raise KeyringInitializationError("Existing credential keyring is invalid.")

    decoded_keys: set[bytes] = set()
    for key_id, encoded_key in encoded_keys.items():
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            raise KeyringInitializationError("Existing credential keyring is invalid.")
        decoded_key = _decode_key(encoded_key)
        if decoded_key in decoded_keys:
            raise KeyringInitializationError("Existing credential keyring is invalid.")
        decoded_keys.add(decoded_key)
    if active_key_id not in encoded_keys:
        raise KeyringInitializationError("Existing credential keyring is invalid.")


def _absolute_path(path: Path) -> Path:
    try:
        result = Path(os.path.abspath(os.fspath(path.expanduser())))
    except (OSError, RuntimeError, TypeError, ValueError):
        raise KeyringInitializationError(
            "Credential keyring path could not be initialized safely."
        ) from None
    if not result.name or result.name in {".", ".."}:
        raise KeyringInitializationError("Credential keyring path must name a regular file.")
    return result


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _unchanged_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _same_inode(first, second)
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _stat_name(parent_descriptor: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)


def _open_parent(path: Path) -> tuple[int, os.stat_result]:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path_snapshot = os.stat(path.parent, follow_symlinks=False)
    except (OSError, ValueError):
        raise KeyringInitializationError(
            "Credential keyring directory could not be initialized safely."
        ) from None
    if not stat.S_ISDIR(path_snapshot.st_mode):
        raise KeyringInitializationError(
            "Credential keyring directory must not be a symbolic link."
        )

    flags = os.O_RDONLY | _OPEN_DIRECTORY | _OPEN_CLOEXEC | _OPEN_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path.parent, flags)
        opened_snapshot = os.fstat(descriptor)
    except (OSError, ValueError):
        if descriptor >= 0:
            os.close(descriptor)
        raise KeyringInitializationError(
            "Credential keyring directory could not be opened safely."
        ) from None
    if not stat.S_ISDIR(opened_snapshot.st_mode) or not _same_inode(path_snapshot, opened_snapshot):
        os.close(descriptor)
        raise KeyringInitializationError(
            "Credential keyring directory changed while it was being opened."
        )
    if hasattr(os, "geteuid") and opened_snapshot.st_uid != os.geteuid():
        os.close(descriptor)
        raise KeyringInitializationError(
            "Credential keyring directory must be owned by the current user."
        )
    if stat.S_IMODE(opened_snapshot.st_mode) & 0o022:
        os.close(descriptor)
        raise KeyringInitializationError(
            "Credential keyring directory must not be group- or world-writable."
        )
    return descriptor, opened_snapshot


def _assert_parent_unchanged(path: Path, opened_snapshot: os.stat_result) -> None:
    try:
        current = os.stat(path.parent, follow_symlinks=False)
    except OSError:
        raise KeyringInitializationError(
            "Credential keyring directory changed during initialization."
        ) from None
    if (
        not stat.S_ISDIR(current.st_mode)
        or not _same_inode(opened_snapshot, current)
        or stat.S_IMODE(current.st_mode) & 0o022
    ):
        raise KeyringInitializationError(
            "Credential keyring directory changed during initialization."
        )


def _read_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = KEYRING_MAX_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _validate_existing(parent_descriptor: int, name: str) -> os.stat_result:
    flags = os.O_RDONLY | _OPEN_CLOEXEC | _OPEN_NOFOLLOW | _OPEN_NONBLOCK
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        raise
    except OSError:
        raise KeyringInitializationError(
            "Credential keyring path must be a regular file, not a symbolic link."
        ) from None

    try:
        opened_snapshot = os.fstat(descriptor)
        try:
            path_snapshot = _stat_name(parent_descriptor, name)
        except OSError:
            raise KeyringInitializationError(
                "Credential keyring path changed while it was being opened."
            ) from None
        if (
            not stat.S_ISREG(opened_snapshot.st_mode)
            or not stat.S_ISREG(path_snapshot.st_mode)
            or not _same_inode(opened_snapshot, path_snapshot)
        ):
            raise KeyringInitializationError(
                "Credential keyring path must be a regular file, not a directory or link."
            )
        if not 0 < opened_snapshot.st_size <= KEYRING_MAX_BYTES:
            raise KeyringInitializationError("Existing credential keyring is invalid.")

        payload = _read_bounded(descriptor)
        after_read = os.fstat(descriptor)
        try:
            current_path = _stat_name(parent_descriptor, name)
        except OSError:
            raise KeyringInitializationError(
                "Credential keyring path changed while it was being validated."
            ) from None
        if not _unchanged_file(opened_snapshot, after_read) or not _same_inode(
            after_read, current_path
        ):
            raise KeyringInitializationError(
                "Credential keyring changed while it was being validated."
            )
        _validate_payload(payload)

        try:
            os.fchmod(descriptor, 0o600)
            secured_snapshot = os.fstat(descriptor)
            secured_path = _stat_name(parent_descriptor, name)
        except OSError:
            raise KeyringInitializationError(
                "Credential keyring permissions could not be secured."
            ) from None
        if (
            not _same_inode(secured_snapshot, secured_path)
            or stat.S_IMODE(secured_snapshot.st_mode) != 0o600
            or secured_snapshot.st_size != after_read.st_size
            or secured_snapshot.st_mtime_ns != after_read.st_mtime_ns
        ):
            raise KeyringInitializationError(
                "Credential keyring changed while its permissions were being secured."
            )
        return secured_snapshot
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short credential keyring write")
        remaining = remaining[written:]


def _unlink_if_same(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result,
) -> None:
    try:
        current = _stat_name(parent_descriptor, name)
        if _same_inode(current, expected):
            os.unlink(name, dir_fd=parent_descriptor)
    except OSError:
        pass


def _atomic_create(
    parent_descriptor: int,
    name: str,
    payload: bytes,
) -> os.stat_result:
    temporary_name = f".credential-keyring-{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN_CLOEXEC | _OPEN_NOFOLLOW
    temporary_descriptor = -1
    temporary_snapshot: os.stat_result | None = None
    installed = False
    try:
        temporary_descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_snapshot = os.fstat(temporary_descriptor)
        os.fchmod(temporary_descriptor, 0o600)
        _write_all(temporary_descriptor, payload)
        os.fsync(temporary_descriptor)
        temporary_snapshot = os.fstat(temporary_descriptor)
        if (
            not stat.S_ISREG(temporary_snapshot.st_mode)
            or stat.S_IMODE(temporary_snapshot.st_mode) != 0o600
            or temporary_snapshot.st_size != len(payload)
        ):
            raise KeyringInitializationError(
                "Credential keyring temporary file could not be secured."
            )
        os.close(temporary_descriptor)
        temporary_descriptor = -1

        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise _DestinationAppeared from None
        installed = True
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)

        installed_snapshot = _stat_name(parent_descriptor, name)
        if not _same_inode(temporary_snapshot, installed_snapshot):
            raise KeyringInitializationError(
                "Credential keyring changed during atomic installation."
            )
        return installed_snapshot
    except BaseException:
        if installed and temporary_snapshot is not None:
            _unlink_if_same(parent_descriptor, name, temporary_snapshot)
        raise
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_snapshot is not None:
            _unlink_if_same(parent_descriptor, temporary_name, temporary_snapshot)


def _new_payload() -> bytes:
    document = {
        "active_key_id": "local-v1",
        "keys": {
            "local-v1": base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).decode("ascii")
        },
    }
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _validate_payload(payload)
    return payload


def ensure_keyring(path: Path) -> bool:
    """Create a valid keyring or validate and secure an existing regular file."""

    absolute_path = _absolute_path(path)
    parent_descriptor = -1
    try:
        parent_descriptor, parent_snapshot = _open_parent(absolute_path)
        try:
            _validate_existing(parent_descriptor, absolute_path.name)
        except FileNotFoundError:
            payload = _new_payload()
            try:
                installed_snapshot = _atomic_create(
                    parent_descriptor,
                    absolute_path.name,
                    payload,
                )
            except _DestinationAppeared:
                _validate_existing(parent_descriptor, absolute_path.name)
                _assert_parent_unchanged(absolute_path, parent_snapshot)
                return False
            try:
                _assert_parent_unchanged(absolute_path, parent_snapshot)
            except BaseException:
                _unlink_if_same(
                    parent_descriptor,
                    absolute_path.name,
                    installed_snapshot,
                )
                raise
            return True
        _assert_parent_unchanged(absolute_path, parent_snapshot)
        return False
    except KeyringInitializationError:
        raise
    except (OSError, ValueError):
        raise KeyringInitializationError(
            "Credential keyring could not be initialized safely."
        ) from None
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".secrets" / "credential-keys.json",
    )
    try:
        created = ensure_keyring(parser.parse_args().path)
    except KeyringInitializationError as error:
        parser.exit(1, f"Error: {error}\n")
    if created:
        status = "Created local credential keyring."
    else:
        status = "Keeping existing credential keyring."
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
