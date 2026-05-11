"""Unit tests for the *_FILE env-var pattern in Settings (M11)."""
from __future__ import annotations

import os
import tempfile

import pytest

from compendium.config.settings import Settings


def _write_secret(tmp_path, content: str) -> str:
    f = tmp_path / "secret"
    f.write_text(content)
    return str(f)


class TestSecretFileLoading:
    def test_jwt_secret_read_from_file(self, tmp_path, monkeypatch):
        path = _write_secret(tmp_path, "my-super-secret-jwt-key-that-is-long-enough-for-the-validator")
        monkeypatch.setenv("COMPENDIUM_JWT_SECRET_KEY_FILE", path)
        monkeypatch.delenv("COMPENDIUM_JWT_SECRET_KEY", raising=False)
        monkeypatch.setenv("COMPENDIUM_ALLOW_INSECURE_JWT", "1")
        s = Settings()
        assert s.jwt_secret_key == "my-super-secret-jwt-key-that-is-long-enough-for-the-validator"

    def test_file_trailing_newline_stripped(self, tmp_path, monkeypatch):
        path = _write_secret(tmp_path, "secret-value\n")
        monkeypatch.setenv("COMPENDIUM_JWT_SECRET_KEY_FILE", path)
        monkeypatch.delenv("COMPENDIUM_JWT_SECRET_KEY", raising=False)
        monkeypatch.setenv("COMPENDIUM_ALLOW_INSECURE_JWT", "1")
        s = Settings()
        assert s.jwt_secret_key == "secret-value"

    def test_direct_env_wins_over_file(self, tmp_path, monkeypatch):
        path = _write_secret(tmp_path, "from-file")
        monkeypatch.setenv("COMPENDIUM_JWT_SECRET_KEY_FILE", path)
        monkeypatch.setenv("COMPENDIUM_JWT_SECRET_KEY", "from-env-and-long-enough-32chars!!")
        s = Settings()
        assert s.jwt_secret_key == "from-env-and-long-enough-32chars!!"

    def test_smtp_password_read_from_file(self, tmp_path, monkeypatch):
        path = _write_secret(tmp_path, "smtp-pass")
        monkeypatch.setenv("COMPENDIUM_SMTP_PASSWORD_FILE", path)
        monkeypatch.delenv("COMPENDIUM_SMTP_PASSWORD", raising=False)
        monkeypatch.setenv("COMPENDIUM_ALLOW_INSECURE_JWT", "1")
        s = Settings()
        assert s.smtp_password == "smtp-pass"

    def test_missing_file_is_silently_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COMPENDIUM_JWT_SECRET_KEY_FILE", "/nonexistent/path/to/secret")
        monkeypatch.delenv("COMPENDIUM_JWT_SECRET_KEY", raising=False)
        monkeypatch.setenv("COMPENDIUM_ALLOW_INSECURE_JWT", "1")
        # Should not raise; falls back to the insecure default (caught by create_app, not Settings).
        s = Settings()
        from compendium.config.settings import INSECURE_JWT_DEFAULT
        assert s.jwt_secret_key == INSECURE_JWT_DEFAULT
