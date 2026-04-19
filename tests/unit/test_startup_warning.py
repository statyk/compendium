"""Unit tests for insecure-JWT startup warning."""

import logging
from unittest.mock import patch

from compendium.config.settings import INSECURE_JWT_DEFAULT, Settings


def test_warning_emitted_when_default_key(caplog):
    settings = Settings(database_url="sqlite:///:memory:", jwt_secret_key=INSECURE_JWT_DEFAULT)
    with patch("compendium.api.app.get_settings", return_value=settings):
        with caplog.at_level(logging.WARNING, logger="compendium"):
            from compendium.api.app import create_app

            create_app()
    assert any("COMPENDIUM_JWT_SECRET_KEY" in r.message for r in caplog.records)


def test_no_warning_when_key_is_set(caplog):
    settings = Settings(database_url="sqlite:///:memory:", jwt_secret_key="a-proper-secret-key")
    with patch("compendium.api.app.get_settings", return_value=settings):
        with caplog.at_level(logging.WARNING, logger="compendium"):
            from compendium.api.app import create_app

            create_app()
    assert not any("COMPENDIUM_JWT_SECRET_KEY" in r.message for r in caplog.records)
