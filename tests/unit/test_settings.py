"""Tests for Settings empty-string env-var coercion.

Docker-compose's `${VAR:-}` pattern passes the var as "" when unset on the
host. The model_validator on Settings must drop those so fields fall back to
their Python defaults instead of failing int/bool/Literal parsing.
"""

import pytest

from compendium.config.settings import Settings


def _settings(monkeypatch, **env: str) -> Settings:
    """Build a Settings instance with specific env vars and no .env file."""
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    # env_file="" disables .env discovery; _env_file kwarg is not a thing in
    # pydantic-settings, so we patch the config temporarily instead.
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    try:
        return Settings()
    finally:
        Settings.model_config["env_file"] = original


class TestEmptyStringFallsToDefault:
    def test_max_upload_bytes_empty_uses_default(self, monkeypatch):
        # Regression: the exact crash reported — compose passes "" when unset.
        s = _settings(monkeypatch, COMPENDIUM_MAX_UPLOAD_BYTES="")
        assert s.max_upload_bytes == 100 * 1024 * 1024

    def test_optional_int_empty_becomes_none(self, monkeypatch):
        s = _settings(monkeypatch, COMPENDIUM_AUDIT_RETENTION_DAYS="")
        assert s.audit_retention_days is None

    def test_optional_str_empty_becomes_none(self, monkeypatch):
        s = _settings(monkeypatch, COMPENDIUM_TMDB_API_KEY="")
        assert s.tmdb_api_key is None

    def test_bool_empty_uses_default(self, monkeypatch):
        s = _settings(monkeypatch, COMPENDIUM_SECURE_COOKIES="")
        assert s.secure_cookies is True

    def test_literal_empty_uses_default(self, monkeypatch):
        s = _settings(monkeypatch, COMPENDIUM_DEFAULT_THEME="")
        assert s.default_theme == "light"

    def test_explicit_value_still_parsed(self, monkeypatch):
        s = _settings(monkeypatch, COMPENDIUM_MAX_UPLOAD_BYTES="52428800")
        assert s.max_upload_bytes == 52428800
