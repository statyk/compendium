"""Encryption-at-rest for DB-stored secrets.

Secrets (SMTP password, API keys, etc.) are stored in the ``app_setting``
table encrypted with Fernet (AES-128-CBC + HMAC-SHA256). The encryption key
comes from ``COMPENDIUM_SECRET_KEY`` in the environment — a Fernet key
(URL-safe base64, 32 bytes decoded). Use ``compendium keygen --secret`` to
generate one.

Stored ciphertext is prefixed with ``enc:v1:`` so the storage layer can
distinguish encrypted rows from plaintext rows (e.g. a setting that existed
before the feature was enabled).

A single canary value is written to the DB the first time a real secret is
encrypted. Subsequent reads verify the canary so a wrong-key deployment gets
a clear error rather than a ``cryptography.InvalidToken`` traceback.

Callers that need graceful degradation (the read path) should catch
``SecretKeyMissingError`` / ``SecretKeyMismatchError`` and fall back to
defaults; they must NOT silently continue with a wrong value.
"""
from __future__ import annotations

import os
from enum import Enum

from cryptography.fernet import Fernet, InvalidToken

_ENV_VAR = "COMPENDIUM_SECRET_KEY"
_CIPHERTEXT_PREFIX = "enc:v1:"
_CANARY_KEY = "_secret_canary"
_CANARY_PLAINTEXT = "compendium-canary-v1"


class SecretKeyMissingError(RuntimeError):
    """Raised when encryption/decryption is attempted but no key is configured."""


class SecretKeyMismatchError(RuntimeError):
    """Raised when the configured key cannot decrypt an existing ciphertext.

    Usually means ``COMPENDIUM_SECRET_KEY`` was rotated without re-encrypting
    stored secrets. Restore the original key, or clear + re-enter all secrets.
    """


class CanaryResult(Enum):
    OK = "ok"
    MISSING = "missing"
    MISMATCH = "mismatch"
    NO_KEY = "no_key"


def secret_key_configured() -> bool:
    return bool(os.environ.get(_ENV_VAR))


def _load_fernet() -> Fernet:
    raw = os.environ.get(_ENV_VAR)
    if not raw:
        raise SecretKeyMissingError(
            f"{_ENV_VAR} is not set. Run 'compendium keygen --secret' to generate one "
            "and add it to your environment."
        )
    try:
        return Fernet(raw.encode() if isinstance(raw, str) else raw)
    except Exception as exc:
        raise SecretKeyMissingError(
            f"{_ENV_VAR} is not a valid Fernet key: {exc}. "
            "Run 'compendium keygen --secret' to generate a new one."
        ) from exc


def encrypt(plaintext: str) -> str:
    """Encrypt *plaintext* and return a prefixed ciphertext string for DB storage."""
    f = _load_fernet()
    token = f.encrypt(plaintext.encode()).decode()
    return f"{_CIPHERTEXT_PREFIX}{token}"


def decrypt(stored: str) -> str:
    """Decrypt a value previously returned by ``encrypt()``.

    Raises ``SecretKeyMissingError`` if the key is not configured, or
    ``SecretKeyMismatchError`` if the key cannot decrypt the value.
    """
    f = _load_fernet()
    if not stored.startswith(_CIPHERTEXT_PREFIX):
        return stored
    token = stored[len(_CIPHERTEXT_PREFIX):]
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretKeyMismatchError(
            f"COMPENDIUM_SECRET_KEY cannot decrypt stored secret. "
            "The key may have been rotated. Restore the original key, or clear "
            "and re-enter the stored secrets."
        ) from exc


def is_encrypted(stored: str) -> bool:
    """True if *stored* looks like an encrypted value from this module."""
    return stored.startswith(_CIPHERTEXT_PREFIX)


def check_canary(session) -> CanaryResult:
    """Verify the canary value against the current key.

    Returns CanaryResult.OK on success, .NO_KEY if no key is configured,
    .MISSING if no canary has been written yet (first use), or .MISMATCH
    if the canary decryption fails (wrong key).
    """
    if not secret_key_configured():
        return CanaryResult.NO_KEY

    from compendium.repositories.sql.site_setting_repository import (
        SqlSiteSettingRepository,
    )

    repo = SqlSiteSettingRepository(session)
    row = repo.get(_CANARY_KEY)
    if row is None:
        return CanaryResult.MISSING
    try:
        plaintext = decrypt(row.value)
    except SecretKeyMissingError:
        return CanaryResult.NO_KEY
    except SecretKeyMismatchError:
        return CanaryResult.MISMATCH
    return CanaryResult.OK if plaintext == _CANARY_PLAINTEXT else CanaryResult.MISMATCH


def write_canary(session) -> None:
    """Encrypt the well-known canary value and persist it to the DB.

    Called the first time a real secret is written, so future key-mismatch
    errors surface a clear diagnostic message.
    """
    from compendium.repositories.sql.site_setting_repository import (
        SqlSiteSettingRepository,
    )

    repo = SqlSiteSettingRepository(session)
    row = repo.get(_CANARY_KEY)
    if row is None:
        repo.upsert(_CANARY_KEY, encrypt(_CANARY_PLAINTEXT))
