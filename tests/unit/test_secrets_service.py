"""Unit tests for services/secrets.py — encryption helpers."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from compendium.services.secrets import (
    CanaryResult,
    SecretKeyMissingError,
    SecretKeyMismatchError,
    decrypt,
    encrypt,
    is_encrypted,
    secret_key_configured,
)

_VALID_KEY = Fernet.generate_key().decode()
_OTHER_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    monkeypatch.delenv("COMPENDIUM_SECRET_KEY", raising=False)


def test_secret_key_configured_false_when_absent():
    assert not secret_key_configured()


def test_secret_key_configured_true_when_set(monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _VALID_KEY)
    assert secret_key_configured()


def test_encrypt_raises_without_key():
    with pytest.raises(SecretKeyMissingError):
        encrypt("secret")


def test_decrypt_raises_without_key(monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _VALID_KEY)
    ciphertext = encrypt("secret")
    monkeypatch.delenv("COMPENDIUM_SECRET_KEY")
    with pytest.raises(SecretKeyMissingError):
        decrypt(ciphertext)


def test_round_trip(monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _VALID_KEY)
    plaintext = "hunter2"
    ciphertext = encrypt(plaintext)
    assert is_encrypted(ciphertext)
    assert ciphertext.startswith("enc:v1:")
    assert decrypt(ciphertext) == plaintext


def test_decrypt_with_wrong_key_raises(monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _VALID_KEY)
    ciphertext = encrypt("secret")
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _OTHER_KEY)
    with pytest.raises(SecretKeyMismatchError):
        decrypt(ciphertext)


def test_decrypt_plaintext_passthrough(monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _VALID_KEY)
    assert decrypt("not-encrypted") == "not-encrypted"


def test_is_encrypted():
    assert not is_encrypted("plaintext")
    assert not is_encrypted("")
    assert is_encrypted("enc:v1:somethingelse")


def test_check_canary_no_key():
    from compendium.services.secrets import check_canary

    result = check_canary(MagicMock())
    assert result == CanaryResult.NO_KEY


_REPO_PATH = "compendium.repositories.sql.site_setting_repository.SqlSiteSettingRepository"


def _mock_repo(get_return):
    repo = MagicMock()
    repo.get.return_value = get_return
    return repo


def test_check_canary_missing_returns_missing(monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _VALID_KEY)
    from compendium.services.secrets import check_canary

    with patch(_REPO_PATH, return_value=_mock_repo(None)):
        result = check_canary(MagicMock())

    assert result == CanaryResult.MISSING


def test_check_canary_ok(monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _VALID_KEY)
    from compendium.services.secrets import _CANARY_PLAINTEXT, check_canary

    canary_row = MagicMock()
    canary_row.value = encrypt(_CANARY_PLAINTEXT)

    with patch(_REPO_PATH, return_value=_mock_repo(canary_row)):
        result = check_canary(MagicMock())

    assert result == CanaryResult.OK


def test_check_canary_mismatch(monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _VALID_KEY)
    from compendium.services.secrets import _CANARY_PLAINTEXT, check_canary

    canary_encrypted_with_other_key = (
        "enc:v1:" + Fernet(_OTHER_KEY.encode()).encrypt(_CANARY_PLAINTEXT.encode()).decode()
    )
    canary_row = MagicMock()
    canary_row.value = canary_encrypted_with_other_key

    with patch(_REPO_PATH, return_value=_mock_repo(canary_row)):
        result = check_canary(MagicMock())

    assert result == CanaryResult.MISMATCH
