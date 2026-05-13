"""Integration tests for `compendium secrets set` with GB key pre-save validation."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from compendium.cli.main import app as cli_app
from compendium.config.seed import seed_defaults
from compendium.domain.models import Base
from compendium.services import site_settings as ss
from compendium.services.metadata import KeyValidationResult

_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture
def s_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def s_session(s_engine):
    factory = sessionmaker(bind=s_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture(autouse=True)
def _env_isolation(s_engine, monkeypatch):
    monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
    ss.invalidate_cache()
    for var in ("COMPENDIUM_SECRET_KEY", "COMPENDIUM_GOOGLE_BOOKS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield
    ss.invalidate_cache()


def _fake_scope(s_session):
    @contextmanager
    def _scope():
        yield s_session
        s_session.commit()
    return _scope


def _run(*args, input_text: str | None = None):
    return CliRunner().invoke(cli_app, list(args), input=input_text)


# ---------------------------------------------------------------------------
# Validation failure aborts save (default)
# ---------------------------------------------------------------------------

def test_invalid_key_aborts_without_force(s_session, monkeypatch):
    """secrets set google_books_api_key with bad value aborts when user answers 'n'."""
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    bad_result = KeyValidationResult(ok=False, reason="keyInvalid")

    with (
        patch("compendium.web.routes.admin_settings._SECRET_VALIDATORS",
              {"google_books_api_key": lambda _: bad_result}),
        patch("compendium.cli.commands.secrets.session_scope", _fake_scope(s_session)),
    ):
        result = _run(
            "secrets", "set", "google_books_api_key", "--value", "bad-key",
            input_text="n\n",
        )

    assert result.exit_code == 1
    assert "Validation failed" in result.output or "keyInvalid" in result.output


def test_invalid_key_saves_when_user_confirms(s_session, monkeypatch):
    """secrets set google_books_api_key aborts but re-prompts and saves when user answers 'y'."""
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    bad_result = KeyValidationResult(ok=False, reason="keyInvalid")

    with (
        patch("compendium.web.routes.admin_settings._SECRET_VALIDATORS",
              {"google_books_api_key": lambda _: bad_result}),
        patch("compendium.cli.commands.secrets.session_scope", _fake_scope(s_session)),
    ):
        result = _run(
            "secrets", "set", "google_books_api_key", "--value", "bad-key",
            input_text="y\n",
        )

    assert result.exit_code == 0
    assert "Stored" in result.output


def test_force_flag_skips_validation(s_session, monkeypatch):
    """--force skips the validator entirely."""
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)

    validator_called = [False]

    def _bad_validator(v):
        validator_called[0] = True
        return KeyValidationResult(ok=False, reason="should not be called")

    with (
        patch("compendium.web.routes.admin_settings._SECRET_VALIDATORS",
              {"google_books_api_key": _bad_validator}),
        patch("compendium.cli.commands.secrets.session_scope", _fake_scope(s_session)),
    ):
        result = _run(
            "secrets", "set", "google_books_api_key", "--value", "forced-key", "--force",
        )

    assert result.exit_code == 0
    assert "Stored" in result.output
    assert not validator_called[0]


def test_valid_key_saves_cleanly(s_session, monkeypatch):
    """A key that passes validation is saved without any 'Save anyway?' prompt."""
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    good_result = KeyValidationResult(ok=True)

    with (
        patch("compendium.web.routes.admin_settings._SECRET_VALIDATORS",
              {"google_books_api_key": lambda _: good_result}),
        patch("compendium.cli.commands.secrets.session_scope", _fake_scope(s_session)),
    ):
        result = _run(
            "secrets", "set", "google_books_api_key", "--value", "good-key",
        )

    assert result.exit_code == 0
    assert "Stored" in result.output
    assert "Save anyway" not in result.output


def test_quota_exhausted_key_warns_but_saves(s_session, monkeypatch):
    """A quota-exhausted key (ok=True, warning set) prints warning but still saves."""
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    quota_result = KeyValidationResult(
        ok=True,
        warning="Quota exhausted; key is valid but temporarily blocked.",
    )

    with (
        patch("compendium.web.routes.admin_settings._SECRET_VALIDATORS",
              {"google_books_api_key": lambda _: quota_result}),
        patch("compendium.cli.commands.secrets.session_scope", _fake_scope(s_session)),
    ):
        result = _run(
            "secrets", "set", "google_books_api_key", "--value", "quota-key",
        )

    assert result.exit_code == 0
    assert "Warning" in result.output
    assert "Stored" in result.output
