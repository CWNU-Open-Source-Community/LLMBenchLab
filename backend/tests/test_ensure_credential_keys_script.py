"""Security regression tests for the dependency-free keyring bootstrap script."""

from __future__ import annotations

import base64
import errno
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.security import CredentialKeyring

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPOSITORY_ROOT / "scripts" / "ensure_credential_keys.py"
_BOOTSTRAP_PATH = _REPOSITORY_ROOT / "scripts" / "bootstrap_credential_keyring.sh"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ensure_credential_keys_script", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


def _encoded_key(fill: int = 1) -> str:
    return base64.urlsafe_b64encode(bytes([fill]) * 32).decode("ascii")


def _valid_payload(fill: int = 1) -> bytes:
    return json.dumps(
        {
            "active_key_id": "local-v1",
            "keys": {"local-v1": _encoded_key(fill)},
        }
    ).encode()


def test_all_local_bootstrap_entrypoints_force_cpython() -> None:
    wrapper = _BOOTSTRAP_PATH.read_text(encoding="utf-8")

    assert stat.S_IMODE(_BOOTSTRAP_PATH.stat().st_mode) & 0o111
    assert "--python 'cpython>=3.11'" in wrapper
    assert '--script "$project_root/scripts/ensure_credential_keys.py"' in wrapper
    assert "--directory" not in wrapper

    entrypoints = {
        _REPOSITORY_ROOT / "scripts" / "setup.sh": 1,
        _REPOSITORY_ROOT / "scripts" / "dev.sh": 1,
        _REPOSITORY_ROOT / "Makefile": 3,
    }
    for path, expected_calls in entrypoints.items():
        contents = path.read_text(encoding="utf-8")
        assert contents.count("./scripts/bootstrap_credential_keyring.sh") == expected_calls
        assert "python3 scripts/ensure_credential_keys.py" not in contents


def test_create_is_private_valid_atomic_and_silent(tmp_path: Path, capsys) -> None:
    path = tmp_path / "private" / "credential-keys.json"

    assert script.ensure_keyring(path) is True

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.iterdir()) == [path]
    assert CredentialKeyring.from_file(path).active_key_id == "local-v1"
    assert capsys.readouterr() == ("", "")


def test_existing_valid_file_is_unchanged_and_chmodded(tmp_path: Path) -> None:
    path = tmp_path / "credential-keys.json"
    payload = _valid_payload()
    path.write_bytes(payload)
    path.chmod(0o644)

    assert script.ensure_keyring(path) is False

    assert path.read_bytes() == payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_main_only_prints_generic_status_not_key_material(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "credential-keys.json"
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT_PATH), "--path", str(path)])

    assert script.main() == 0
    created_output = capsys.readouterr()
    secret_value = next(iter(json.loads(path.read_text(encoding="utf-8"))["keys"].values()))

    assert secret_value not in created_output.out
    assert secret_value not in created_output.err
    assert script.main() == 0
    existing_output = capsys.readouterr()
    assert secret_value not in existing_output.out
    assert secret_value not in existing_output.err


def test_rejects_symbolic_link_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(_valid_payload())
    target.chmod(0o644)
    link = tmp_path / "credential-keys.json"
    link.symlink_to(target)

    with pytest.raises(script.KeyringInitializationError, match="regular file"):
        script.ensure_keyring(link)

    assert link.is_symlink()
    assert target.read_bytes() == _valid_payload()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_rejects_directory_at_keyring_path(tmp_path: Path) -> None:
    path = tmp_path / "credential-keys.json"
    path.mkdir()

    with pytest.raises(script.KeyringInitializationError, match="regular file"):
        script.ensure_keyring(path)


def test_rejects_symbolic_link_directory(tmp_path: Path) -> None:
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)

    with pytest.raises(script.KeyringInitializationError, match="directory"):
        script.ensure_keyring(linked_parent / "credential-keys.json")

    assert not (actual_parent / "credential-keys.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not-json",
        b"\xff",
        b"[]",
        b'{"active_key_id":"local-v1","keys":{},"extra":true}',
        b'{"active_key_id":"one","active_key_id":"two","keys":{}}',
        b'{"active_key_id":"local-v1","keys":{"local-v1":"short"}}',
        json.dumps(
            {
                "active_key_id": "local-v1",
                "keys": {
                    "local-v1": _encoded_key(),
                    "duplicate": _encoded_key(),
                },
            }
        ).encode(),
    ],
)
def test_rejects_invalid_existing_keyring_without_echo(
    tmp_path: Path,
    payload: bytes,
) -> None:
    path = tmp_path / "credential-keys.json"
    path.write_bytes(payload)

    with pytest.raises(script.KeyringInitializationError) as caught:
        script.ensure_keyring(path)

    rendered = f"{caught.value!s} {caught.value!r}"
    decoded_payload = payload.decode("utf-8", errors="ignore")
    if decoded_payload:
        assert decoded_payload not in rendered


def test_rejects_oversized_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "credential-keys.json"
    path.write_bytes(b"x" * (script.KEYRING_MAX_BYTES + 1))

    with pytest.raises(script.KeyringInitializationError, match="invalid"):
        script.ensure_keyring(path)


def test_detects_path_swap_before_chmod(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "credential-keys.json"
    moved = tmp_path / "opened-original.json"
    path.write_bytes(_valid_payload(1))
    path.chmod(0o644)
    original_read = script._read_bounded

    def swap_path(descriptor: int) -> bytes:
        payload = original_read(descriptor)
        path.rename(moved)
        path.write_bytes(_valid_payload(2))
        path.chmod(0o644)
        return payload

    monkeypatch.setattr(script, "_read_bounded", swap_path)

    with pytest.raises(script.KeyringInitializationError, match="changed"):
        script.ensure_keyring(path)

    assert path.read_bytes() == _valid_payload(2)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert stat.S_IMODE(moved.stat().st_mode) == 0o644


def test_failed_write_leaves_no_destination_or_temporary_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "credential-keys.json"
    marker = b"partial-key-material-that-must-not-remain"

    def fail_after_partial_write(descriptor: int, _payload: bytes) -> None:
        os.write(descriptor, marker)
        raise OSError("injected write failure")

    monkeypatch.setattr(script, "_write_all", fail_after_partial_write)

    with pytest.raises(script.KeyringInitializationError):
        script.ensure_keyring(path)

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_create_retries_a_cleaned_os_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "credential-keys.json"
    original_link = script.os.link
    attempts = 0
    delays: list[float] = []

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(errno.EBUSY, "sensitive operating-system detail")
        return original_link(*args, **kwargs)

    monkeypatch.setattr(script.os, "link", fail_once)
    monkeypatch.setattr(script.time, "sleep", delays.append)

    assert script.ensure_keyring(path) is True
    assert attempts == 2
    assert delays == [script._ATOMIC_CREATE_RETRY_SECONDS]
    assert CredentialKeyring.from_file(path).active_key_id == "local-v1"
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_create_reports_nonretryable_errno_without_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "credential-keys.json"
    attempts = 0
    sensitive_detail = "sensitive path and operating-system detail"

    def always_fail(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError(errno.EINVAL, sensitive_detail)

    monkeypatch.setattr(script, "_atomic_create", always_fail)
    monkeypatch.setattr(script.time, "sleep", lambda _delay: None)

    with pytest.raises(script.KeyringInitializationError, match="EINVAL") as caught:
        script.ensure_keyring(path)

    assert attempts == 1
    assert sensitive_detail not in str(caught.value)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_create_does_not_retry_when_temporary_cleanup_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "credential-keys.json"
    sensitive_detail = "sensitive cleanup operating-system detail"
    link_attempts = 0
    original_unlink = script.os.unlink

    def transient_link_failure(*_args, **_kwargs):
        nonlocal link_attempts
        link_attempts += 1
        raise OSError(errno.EBUSY, "transient link failure")

    def refuse_temporary_cleanup(name, *args, **kwargs):
        if str(name).startswith(".credential-keyring-"):
            raise OSError(errno.EACCES, sensitive_detail)
        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(script.os, "link", transient_link_failure)
    monkeypatch.setattr(script.os, "unlink", refuse_temporary_cleanup)
    monkeypatch.setattr(script.time, "sleep", lambda _delay: pytest.fail("must not retry"))

    with pytest.raises(script.KeyringInitializationError, match="EACCES") as caught:
        script.ensure_keyring(path)

    assert link_attempts == 1
    assert sensitive_detail not in str(caught.value)
    assert not path.exists()
    temporary_files = list(tmp_path.glob(".credential-keyring-*.tmp"))
    assert len(temporary_files) == 1

    original_unlink(temporary_files[0])
    assert list(tmp_path.iterdir()) == []


def test_atomic_create_does_not_retry_when_temporary_close_is_interrupted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "credential-keys.json"
    sensitive_detail = "sensitive close operating-system detail"
    original_open = script.os.open
    original_close = script.os.close
    temporary_descriptor: int | None = None
    close_interrupted = False

    def track_temporary_descriptor(name, *args, **kwargs):
        nonlocal temporary_descriptor
        descriptor = original_open(name, *args, **kwargs)
        if str(name).startswith(".credential-keyring-"):
            temporary_descriptor = descriptor
        return descriptor

    def interrupt_temporary_close(descriptor: int) -> None:
        nonlocal close_interrupted
        if descriptor == temporary_descriptor and not close_interrupted:
            close_interrupted = True
            raise OSError(errno.EINTR, sensitive_detail)
        original_close(descriptor)

    monkeypatch.setattr(script.os, "open", track_temporary_descriptor)
    monkeypatch.setattr(script.os, "close", interrupt_temporary_close)
    monkeypatch.setattr(script.time, "sleep", lambda _delay: pytest.fail("must not retry"))

    with pytest.raises(script.KeyringInitializationError, match="EINTR") as caught:
        script.ensure_keyring(path)

    assert close_interrupted is True
    assert sensitive_detail not in str(caught.value)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []
    assert temporary_descriptor is not None
    original_close(temporary_descriptor)


def test_atomic_create_does_not_retry_when_open_result_is_uncertain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "credential-keys.json"
    sensitive_detail = "sensitive open operating-system detail"
    original_open = script.os.open
    original_close = script.os.close
    temporary_open_attempts = 0

    def create_then_report_failure(name, flags, *args, **kwargs):
        nonlocal temporary_open_attempts
        if str(name).startswith(".credential-keyring-"):
            temporary_open_attempts += 1
            descriptor = original_open(name, flags, *args, **kwargs)
            original_close(descriptor)
            raise OSError(errno.EBUSY, sensitive_detail)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(script.os, "open", create_then_report_failure)
    monkeypatch.setattr(script.time, "sleep", lambda _delay: pytest.fail("must not retry"))

    with pytest.raises(script.KeyringInitializationError, match="EBUSY") as caught:
        script.ensure_keyring(path)

    assert temporary_open_attempts == 1
    assert sensitive_detail not in str(caught.value)
    assert not path.exists()
    uncertain_files = list(tmp_path.glob(".credential-keyring-*.tmp"))
    assert len(uncertain_files) == 1
    assert uncertain_files[0].stat().st_size == 0

    original_unlink = os.unlink
    original_unlink(uncertain_files[0])
    assert list(tmp_path.iterdir()) == []


def test_concurrent_valid_creator_is_validated_and_secured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "credential-keys.json"

    def win_install_race(*args, **kwargs) -> None:
        path.write_bytes(_valid_payload())
        path.chmod(0o644)
        raise FileExistsError

    monkeypatch.setattr(script.os, "link", win_install_race)

    assert script.ensure_keyring(path) is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert CredentialKeyring.from_file(path).active_key_id == "local-v1"
