"""Unit tests for insecure-JWT startup behavior (H3)."""

import logging
import os
from unittest.mock import patch

import pytest

from compendium.config.settings import INSECURE_JWT_DEFAULT, InsecureConfigError, Settings


def test_create_app_raises_when_default_key_and_no_escape_hatch(monkeypatch):
    monkeypatch.delenv("COMPENDIUM_ALLOW_INSECURE_JWT", raising=False)
    settings = Settings(database_url="sqlite:///:memory:", jwt_secret_key=INSECURE_JWT_DEFAULT)
    with patch("compendium.api.app.get_settings", return_value=settings):
        from compendium.api.app import create_app

        with pytest.raises(InsecureConfigError, match="COMPENDIUM_JWT_SECRET_KEY"):
            create_app()


def test_create_app_warns_when_default_key_and_escape_hatch_set(monkeypatch, caplog):
    monkeypatch.setenv("COMPENDIUM_ALLOW_INSECURE_JWT", "1")
    settings = Settings(database_url="sqlite:///:memory:", jwt_secret_key=INSECURE_JWT_DEFAULT)
    with patch("compendium.api.app.get_settings", return_value=settings):
        with caplog.at_level(logging.WARNING, logger="compendium"):
            from compendium.api.app import create_app

            create_app()
    assert any("COMPENDIUM_JWT_SECRET_KEY" in r.message for r in caplog.records)
    assert any("DO NOT" in r.message for r in caplog.records)


def test_create_app_silent_when_real_key_set(monkeypatch, caplog):
    # Escape hatch presence shouldn't matter — a real key is a real key.
    monkeypatch.setenv("COMPENDIUM_ALLOW_INSECURE_JWT", "1")
    settings = Settings(
        database_url="sqlite:///:memory:",
        jwt_secret_key="a-proper-secret-key-that-is-long-enough-abcdef",
    )
    with patch("compendium.api.app.get_settings", return_value=settings):
        with caplog.at_level(logging.WARNING, logger="compendium"):
            from compendium.api.app import create_app

            create_app()
    assert not any("COMPENDIUM_JWT_SECRET_KEY" in r.message for r in caplog.records)


def test_escape_hatch_is_strict_one(monkeypatch):
    # "true", "yes", anything other than "1" doesn't bypass.
    monkeypatch.setenv("COMPENDIUM_ALLOW_INSECURE_JWT", "true")
    settings = Settings(database_url="sqlite:///:memory:", jwt_secret_key=INSECURE_JWT_DEFAULT)
    with patch("compendium.api.app.get_settings", return_value=settings):
        from compendium.api.app import create_app

        with pytest.raises(InsecureConfigError):
            create_app()


def test_create_app_raises_when_key_too_short(monkeypatch):
    monkeypatch.delenv("COMPENDIUM_ALLOW_INSECURE_JWT", raising=False)
    settings = Settings(database_url="sqlite:///:memory:", jwt_secret_key="tooshort")
    with patch("compendium.api.app.get_settings", return_value=settings):
        from compendium.api.app import create_app

        with pytest.raises(InsecureConfigError, match="shorter than the"):
            create_app()


def test_create_app_warns_when_key_too_short_and_escape_hatch(monkeypatch, caplog):
    monkeypatch.setenv("COMPENDIUM_ALLOW_INSECURE_JWT", "1")
    settings = Settings(database_url="sqlite:///:memory:", jwt_secret_key="tooshort")
    with patch("compendium.api.app.get_settings", return_value=settings):
        with caplog.at_level(logging.WARNING, logger="compendium"):
            from compendium.api.app import create_app

            create_app()
    assert any("shorter than the" in r.message for r in caplog.records)
