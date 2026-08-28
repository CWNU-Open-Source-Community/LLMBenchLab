"""Security primitives that do not depend on persistence or HTTP layers."""

from .credentials import (
    CREDENTIAL_ALGORITHM,
    CredentialCryptoError,
    CredentialDecryptionError,
    CredentialEnvelopeError,
    CredentialInputError,
    CredentialKeyring,
    CredentialKeyringInvalidError,
    CredentialKeyringUnavailableError,
    CredentialKeyUnavailableError,
    EncryptedCredential,
    normalize_provider_origin,
)
from .provider_metadata import normalize_http_attempt_count, normalize_provider_metadata

__all__ = [
    "CREDENTIAL_ALGORITHM",
    "CredentialCryptoError",
    "CredentialDecryptionError",
    "CredentialEnvelopeError",
    "CredentialInputError",
    "CredentialKeyUnavailableError",
    "CredentialKeyring",
    "CredentialKeyringInvalidError",
    "CredentialKeyringUnavailableError",
    "EncryptedCredential",
    "normalize_http_attempt_count",
    "normalize_provider_metadata",
    "normalize_provider_origin",
]
