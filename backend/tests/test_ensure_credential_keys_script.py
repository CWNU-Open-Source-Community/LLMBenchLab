"""Security regression tests for the dependency-free keyring bootstrap script."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.security import CredentialKeyring

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ensure_credential_keys.py"


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
