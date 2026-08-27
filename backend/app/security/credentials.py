"""Application-level encryption for persisted Provider credentials.

The module deliberately has no ORM, API, or process-environment dependencies.
Callers supply the Model identifier and the Base URL from the immutable Run
snapshot when decrypting so authenticated data prevents credential forwarding
across models or Provider origins.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

CREDENTIAL_ALGORITHM: Final = "aes-256-gcm-v1"
KEYRING_MAX_BYTES: Final = 64 * 1024
KEYRING_MAX_KEYS: Final = 32
API_KEY_MAX_BYTES: Final = 8 * 1024
NONCE_BYTES: Final = 12
_KEY_BYTES: Final = 32
_KEY_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BASE64URL_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43}=?$")


class CredentialCryptoError(RuntimeError):
    """Base class carrying a stable, non-sensitive machine-readable code."""

    code = "credential_crypto_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class CredentialKeyringUnavailableError(CredentialCryptoError):
    """The configured keyring file is missing or cannot be read."""

    code = "credential_keyring_unavailable"


class CredentialKeyringInvalidError(CredentialCryptoError):
    """The keyring bytes do not satisfy the strict file schema."""

    code = "credential_keyring_invalid"


class CredentialKeyUnavailableError(CredentialCryptoError):
    """A ciphertext names a key ID absent from this process's keyring."""

    code = "credential_key_unavailable"


class CredentialDecryptionError(CredentialCryptoError):
    """Ciphertext authentication or plaintext decoding failed."""

    code = "credential_decryption_failed"


class CredentialEnvelopeError(CredentialCryptoError):
    """Persisted encrypted fields are structurally invalid or unsupported."""

    code = "credential_envelope_invalid"


class CredentialInputError(CredentialCryptoError):
    """A secret, Model identifier, or Provider URL is invalid."""

    code = "credential_input_invalid"


class _DuplicateJSONKey(ValueError):
    """Private JSON parsing signal; its message is never propagated."""


@dataclass(frozen=True, slots=True, repr=False)
class EncryptedCredential:
    """Opaque columns suitable for persistence outside public schemas."""

    key_id: str
    algorithm: str
    nonce: bytes
    ciphertext: bytes

    def __repr__(self) -> str:
        return "EncryptedCredential([REDACTED])"


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


def _decode_key(value: object) -> bytes:
    if not isinstance(value, str) or not _BASE64URL_KEY_RE.fullmatch(value):
        raise CredentialKeyringInvalidError
    unpadded = value.rstrip("=")
    padded = unpadded + "=" * (-len(unpadded) % 4)
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise CredentialKeyringInvalidError from None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if len(decoded) != _KEY_BYTES or canonical != unpadded:
        raise CredentialKeyringInvalidError
    return decoded


def _read_keyring_file(path: Path | None) -> bytes:
    if path is None:
        raise CredentialKeyringUnavailableError
    try:
        with path.open("rb") as keyring_file:
            payload = keyring_file.read(KEYRING_MAX_BYTES + 1)
    except (OSError, ValueError, TypeError):
        raise CredentialKeyringUnavailableError from None
    if not payload or len(payload) > KEYRING_MAX_BYTES:
        raise CredentialKeyringInvalidError
    return payload


def _parse_keyring(payload: bytes) -> tuple[str, dict[str, bytes]]:
    try:
        decoded = payload.decode("utf-8")
        document = json.loads(decoded, object_pairs_hook=_strict_json_object)
    except (UnicodeError, json.JSONDecodeError, _DuplicateJSONKey):
        raise CredentialKeyringInvalidError from None
    if not isinstance(document, dict) or set(document) != {"active_key_id", "keys"}:
        raise CredentialKeyringInvalidError

    active_key_id = document["active_key_id"]
    encoded_keys = document["keys"]
    if not isinstance(active_key_id, str) or not _KEY_ID_RE.fullmatch(active_key_id):
        raise CredentialKeyringInvalidError
    if (
        not isinstance(encoded_keys, dict)
        or not encoded_keys
        or len(encoded_keys) > KEYRING_MAX_KEYS
    ):
        raise CredentialKeyringInvalidError

    keys: dict[str, bytes] = {}
    seen_material: set[bytes] = set()
    for key_id, encoded_key in encoded_keys.items():
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            raise CredentialKeyringInvalidError
        key = _decode_key(encoded_key)
        if key in seen_material:
            raise CredentialKeyringInvalidError
        seen_material.add(key)
        keys[key_id] = key
    if active_key_id not in keys:
        raise CredentialKeyringInvalidError
    return active_key_id, keys


def normalize_provider_origin(base_url: str) -> str:
    """Return a deterministic HTTP(S) origin without path, query, or fragment."""

    if not isinstance(base_url, str):
        raise CredentialInputError
    normalized = base_url.strip()
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise CredentialInputError from None
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CredentialInputError

    raw_host = hostname.rstrip(".")
    if not raw_host:
        raise CredentialInputError
    try:
        ip = ipaddress.ip_address(raw_host)
    except ValueError:
        try:
            canonical_host = raw_host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise CredentialInputError from None
        if not canonical_host or any(character.isspace() for character in canonical_host):
            raise CredentialInputError from None
        authority = canonical_host
    else:
        canonical_host = ip.compressed.lower()
        authority = f"[{canonical_host}]" if ip.version == 6 else canonical_host

    default_port = 80 if scheme == "http" else 443
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def _associated_data(*, model_id: str, provider_base_url: str) -> bytes:
    if not isinstance(model_id, str) or not 1 <= len(model_id) <= 128 or not model_id.strip():
        raise CredentialInputError
    origin = normalize_provider_origin(provider_base_url)
    return json.dumps(
        {
            "algorithm": CREDENTIAL_ALGORITHM,
            "model_id": model_id,
            "provider_origin": origin,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class CredentialKeyring:
    """An immutable keyring that encrypts with one active key and reads all keys."""

    __slots__ = ("_active_key_id", "_keys")

    def __init__(self, active_key_id: str, keys: dict[str, bytes]) -> None:
        # Construction is intentionally private-by-convention; callers should use
        # ``from_file`` so every production key passes the strict parser.
        self._active_key_id = active_key_id
        self._keys = MappingProxyType(dict(keys))

    @classmethod
    def from_file(cls, path: Path | str | None) -> CredentialKeyring:
        """Load a bounded, duplicate-safe JSON keyring from disk."""

        resolved_path = Path(path) if isinstance(path, str) else path
        active_key_id, keys = _parse_keyring(_read_keyring_file(resolved_path))
        return cls(active_key_id, keys)

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    def encrypt(
        self,
        secret: SecretStr,
        *,
        model_id: str,
        provider_base_url: str,
    ) -> EncryptedCredential:
        """Encrypt one nonempty Provider credential for a Model and origin."""

        if not isinstance(secret, SecretStr):
            raise CredentialInputError
        try:
            plaintext = secret.get_secret_value().encode("utf-8")
        except UnicodeError:
            raise CredentialInputError from None
        if not plaintext or len(plaintext) > API_KEY_MAX_BYTES:
            raise CredentialInputError
        aad = _associated_data(model_id=model_id, provider_base_url=provider_base_url)
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(self._keys[self._active_key_id]).encrypt(nonce, plaintext, aad)
        return EncryptedCredential(
            key_id=self._active_key_id,
            algorithm=CREDENTIAL_ALGORITHM,
            nonce=nonce,
            ciphertext=ciphertext,
        )

    def decrypt(
        self,
        encrypted: EncryptedCredential,
        *,
        model_id: str,
        provider_base_url: str,
    ) -> SecretStr:
        """Authenticate and decrypt a Provider credential from persisted columns."""

        if not isinstance(encrypted, EncryptedCredential):
            raise CredentialEnvelopeError
        if (
            not isinstance(encrypted.key_id, str)
            or not _KEY_ID_RE.fullmatch(encrypted.key_id)
            or encrypted.algorithm != CREDENTIAL_ALGORITHM
        ):
            raise CredentialEnvelopeError
        key = self._keys.get(encrypted.key_id)
        if key is None:
            raise CredentialKeyUnavailableError
        if (
            not isinstance(encrypted.nonce, bytes)
            or len(encrypted.nonce) != NONCE_BYTES
            or not isinstance(encrypted.ciphertext, bytes)
            or len(encrypted.ciphertext) <= 16
            or len(encrypted.ciphertext) > API_KEY_MAX_BYTES + 16
        ):
            raise CredentialEnvelopeError
        aad = _associated_data(model_id=model_id, provider_base_url=provider_base_url)
        try:
            plaintext = AESGCM(key).decrypt(encrypted.nonce, encrypted.ciphertext, aad)
            value = plaintext.decode("utf-8")
        except (InvalidTag, UnicodeError, ValueError):
            raise CredentialDecryptionError from None
        if not value or len(plaintext) > API_KEY_MAX_BYTES:
            raise CredentialDecryptionError
        return SecretStr(value)
