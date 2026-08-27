"""Credential keyring parsing, AEAD binding, and secret-safe failures."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.db.session import create_database_engine
from app.security.credentials import (
    API_KEY_MAX_BYTES,
    CREDENTIAL_ALGORITHM,
    KEYRING_MAX_BYTES,
    CredentialDecryptionError,
    CredentialEnvelopeError,
    CredentialInputError,
    CredentialKeyring,
    CredentialKeyringInvalidError,
    CredentialKeyringUnavailableError,
    CredentialKeyUnavailableError,
    normalize_provider_origin,
)


def _encoded_key(fill: int) -> str:
    return base64.urlsafe_b64encode(bytes([fill]) * 32).decode("ascii")


def _write_keyring(
    path: Path,
    *,
    active_key_id: str = "primary-2026",
    keys: dict[str, str] | None = None,
) -> Path:
    document = {
        "active_key_id": active_key_id,
        "keys": keys or {active_key_id: _encoded_key(1)},
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _assert_safe_error(error: Exception, *sensitive_values: str) -> None:
    rendered = f"{error!s} {error!r}"
    for value in sensitive_values:
        if value:
            assert value not in rendered
    assert error.__cause__ is None


def test_encrypt_decrypt_round_trip_is_opaque_and_uses_random_nonces(tmp_path: Path) -> None:
    keyring = CredentialKeyring.from_file(_write_keyring(tmp_path / "keys.json"))
    secret_value = "fixture-provider-secret"
    secret = SecretStr(secret_value)

    first = keyring.encrypt(
        secret,
        model_id="model-1",
        provider_base_url="https://Provider.Example:443/v1/chat/completions",
    )
    second = keyring.encrypt(
        secret,
        model_id="model-1",
        provider_base_url="https://provider.example/another-path",
    )

    assert first.algorithm == CREDENTIAL_ALGORITHM
    assert first.key_id == "primary-2026"
    assert len(first.nonce) == 12
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert secret_value.encode() not in first.ciphertext
    assert secret_value not in repr(first)
    decrypted = keyring.decrypt(
        first,
        model_id="model-1",
        provider_base_url="https://provider.example/v1",
    )
    assert isinstance(decrypted, SecretStr)
    assert decrypted.get_secret_value() == secret_value
    assert secret_value not in repr(decrypted)


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (" HTTPS://Example.COM.:443/v1 ", "https://example.com"),
        ("http://localhost.:80/v1", "http://localhost"),
        ("https://example.com:8443/v1", "https://example.com:8443"),
        ("https://[2001:0db8::1]:443/v1", "https://[2001:db8::1]"),
        ("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080"),
    ],
)
def test_normalize_provider_origin_is_canonical(base_url: str, expected: str) -> None:
    assert normalize_provider_origin(base_url) == expected


@pytest.mark.parametrize(
    "base_url",
    [
        "provider.example/v1",
        "ftp://provider.example/v1",
        "https://user:password@provider.example/v1",
        "https://provider.example/v1?token=value",
        "https://provider.example/v1#fragment",
        "https://provider.example:invalid/v1",
        "https://",
    ],
)
def test_normalize_provider_origin_rejects_invalid_urls_without_echo(base_url: str) -> None:
    with pytest.raises(CredentialInputError) as caught:
        normalize_provider_origin(base_url)
    _assert_safe_error(caught.value, base_url)


@pytest.mark.parametrize(
    ("model_id", "base_url"),
    [
        ("other-model", "https://provider.example/v1"),
        ("model-1", "https://other-provider.example/v1"),
        ("model-1", "https://provider.example:8443/v1"),
    ],
)
def test_decrypt_rejects_wrong_authenticated_context(
    tmp_path: Path,
    model_id: str,
    base_url: str,
) -> None:
    keyring = CredentialKeyring.from_file(_write_keyring(tmp_path / "keys.json"))
    encrypted = keyring.encrypt(
        SecretStr("context-bound-secret"),
        model_id="model-1",
        provider_base_url="https://provider.example/v1",
    )

    with pytest.raises(CredentialDecryptionError) as caught:
        keyring.decrypt(encrypted, model_id=model_id, provider_base_url=base_url)

    _assert_safe_error(caught.value, "context-bound-secret", model_id, base_url)


def test_decrypt_rejects_tamper_wrong_key_and_unknown_key_id(tmp_path: Path) -> None:
    keyring = CredentialKeyring.from_file(_write_keyring(tmp_path / "keys.json"))
    encrypted = keyring.encrypt(
        SecretStr("tamper-marker"),
        model_id="model-1",
        provider_base_url="https://provider.example/v1",
    )
    tampered = replace(
        encrypted,
        ciphertext=encrypted.ciphertext[:-1] + bytes([encrypted.ciphertext[-1] ^ 1]),
    )
    with pytest.raises(CredentialDecryptionError) as tamper_error:
        keyring.decrypt(
            tampered,
            model_id="model-1",
            provider_base_url="https://provider.example/v1",
        )
    _assert_safe_error(tamper_error.value, "tamper-marker")

    wrong_keyring = CredentialKeyring.from_file(
        _write_keyring(
            tmp_path / "wrong.json",
            keys={"primary-2026": _encoded_key(2)},
        )
    )
    with pytest.raises(CredentialDecryptionError):
        wrong_keyring.decrypt(
            encrypted,
            model_id="model-1",
            provider_base_url="https://provider.example/v1",
        )

    unknown = replace(encrypted, key_id="retired-key")
    with pytest.raises(CredentialKeyUnavailableError) as unknown_error:
        keyring.decrypt(
            unknown,
            model_id="model-1",
            provider_base_url="https://provider.example/v1",
        )
    _assert_safe_error(unknown_error.value, "retired-key", "tamper-marker")

    malformed_key_id = replace(encrypted, key_id="invalid key id containing spaces")
    with pytest.raises(CredentialEnvelopeError) as malformed_error:
        keyring.decrypt(
            malformed_key_id,
            model_id="model-1",
            provider_base_url="https://provider.example/v1",
        )
    _assert_safe_error(malformed_error.value, malformed_key_id.key_id)


@pytest.mark.parametrize(
    "mutation",
    [
        {"algorithm": "unsupported"},
        {"nonce": b"short"},
        {"ciphertext": b"short"},
    ],
)
def test_decrypt_rejects_malformed_envelopes(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    keyring = CredentialKeyring.from_file(_write_keyring(tmp_path / "keys.json"))
    encrypted = keyring.encrypt(
        SecretStr("envelope-marker"),
        model_id="model-1",
        provider_base_url="https://provider.example/v1",
    )

    with pytest.raises(CredentialEnvelopeError) as caught:
        keyring.decrypt(
            replace(encrypted, **mutation),
            model_id="model-1",
            provider_base_url="https://provider.example/v1",
        )

    _assert_safe_error(caught.value, "envelope-marker", repr(mutation))


def test_keyring_missing_unreadable_empty_and_oversized_files_fail_safely(
    tmp_path: Path,
) -> None:
    with pytest.raises(CredentialKeyringUnavailableError):
        CredentialKeyring.from_file(None)
    missing = tmp_path / "missing-secret-name.json"
    with pytest.raises(CredentialKeyringUnavailableError) as missing_error:
        CredentialKeyring.from_file(missing)
    _assert_safe_error(missing_error.value, str(missing))
    with pytest.raises(CredentialKeyringUnavailableError):
        CredentialKeyring.from_file(tmp_path)

    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(CredentialKeyringInvalidError):
        CredentialKeyring.from_file(empty)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (KEYRING_MAX_BYTES + 1))
    with pytest.raises(CredentialKeyringInvalidError):
        CredentialKeyring.from_file(oversized)


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"\xff",
        b"[]",
        b'{"active_key_id":"one","keys":{},"extra":true}',
        b'{"active_key_id":"one","active_key_id":"two","keys":{}}',
        b'{"active_key_id":"one","keys":{"one":"abc","one":"def"}}',
    ],
)
def test_keyring_rejects_invalid_json_shapes_and_duplicate_fields(
    tmp_path: Path,
    payload: bytes,
) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(payload)
    with pytest.raises(CredentialKeyringInvalidError) as caught:
        CredentialKeyring.from_file(path)
    _assert_safe_error(caught.value, payload.decode("utf-8", errors="ignore"))


@pytest.mark.parametrize(
    ("active_key_id", "keys"),
    [
        ("missing", {"present": _encoded_key(1)}),
        ("bad key id", {"bad key id": _encoded_key(1)}),
        ("primary", {"primary": "not-base64url"}),
        ("primary", {"primary": base64.urlsafe_b64encode(b"short").decode("ascii")}),
        ("primary", {"primary": _encoded_key(1), "alias": _encoded_key(1)}),
    ],
)
def test_keyring_rejects_invalid_ids_lengths_and_duplicate_key_material(
    tmp_path: Path,
    active_key_id: str,
    keys: dict[str, str],
) -> None:
    path = _write_keyring(
        tmp_path / "invalid-key.json",
        active_key_id=active_key_id,
        keys=keys,
    )
    with pytest.raises(CredentialKeyringInvalidError):
        CredentialKeyring.from_file(path)


def test_keyring_accepts_padded_and_unpadded_base64url_keys(tmp_path: Path) -> None:
    padded = _encoded_key(1)
    keyring = CredentialKeyring.from_file(
        _write_keyring(
            tmp_path / "keys.json",
            active_key_id="unpadded",
            keys={"padded": padded, "unpadded": _encoded_key(2).rstrip("=")},
        )
    )
    assert keyring.active_key_id == "unpadded"
    assert keyring.key_ids == frozenset({"padded", "unpadded"})


def test_encrypt_requires_secretstr_and_bounds_plaintext(tmp_path: Path) -> None:
    keyring = CredentialKeyring.from_file(_write_keyring(tmp_path / "keys.json"))
    with pytest.raises(CredentialInputError):
        keyring.encrypt(  # type: ignore[arg-type]
            "plain-string",
            model_id="model-1",
            provider_base_url="https://provider.example/v1",
        )
    for value in ("", "x" * (API_KEY_MAX_BYTES + 1)):
        with pytest.raises(CredentialInputError) as caught:
            keyring.encrypt(
                SecretStr(value),
                model_id="model-1",
                provider_base_url="https://provider.example/v1",
            )
        _assert_safe_error(caught.value, value)


def test_settings_loads_optional_credential_keyring_path(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    monkeypatch.setenv("LLMBENCHLAB_CREDENTIAL_KEYS_FILE", str(path))
    settings = Settings(_env_file=None)
    assert settings.credential_keys_file == path

    monkeypatch.setenv("LLMBENCHLAB_CREDENTIAL_KEYS_FILE", "")
    assert Settings(_env_file=None).credential_keys_file is None


def test_settings_uses_repository_keyring_when_legacy_env_has_no_new_field(
    monkeypatch,
) -> None:
    monkeypatch.delenv("LLMBENCHLAB_CREDENTIAL_KEYS_FILE", raising=False)

    settings = Settings(_env_file=None)

    assert settings.credential_keys_file == (
        Path(__file__).resolve().parents[2] / ".secrets" / "credential-keys.json"
    )


def test_database_engine_hides_bound_parameters() -> None:
    database_engine = create_database_engine("sqlite://")
    try:
        assert database_engine.hide_parameters is True
    finally:
        database_engine.dispose()
