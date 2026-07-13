"""Destructive commands prompt unless --yes; 'n' aborts without changes."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from compendium.cli.main import app
from compendium.config.seed import seed_defaults
from compendium.domain.models import Base
from compendium.services import site_settings as ss

runner = CliRunner()


@pytest.fixture
def cli_db(monkeypatch):
    """Route every command's session_scope() at a shared in-memory DB.

    Unlike the ``session`` fixture used elsewhere (which patches
    ``session_scope`` per-module to yield one shared session), these tests
    invoke the CLI directly with no per-command patching, so each command's
    own ``session_scope()`` call must resolve to the same engine. StaticPool
    keeps the single in-memory connection alive across those separate
    sessions.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    seed_session = factory()
    seed_defaults(seed_session)
    seed_session.commit()
    seed_session.close()

    # db/session.py does ``from compendium.db.engine import get_engine`` at
    # module level, so it holds its own reference — patching
    # ``compendium.db.engine.get_engine`` alone would not be honored here.
    # Patch both so every consumer (session_scope() and anything that looks
    # up engine_mod.get_engine() fresh) sees the same test engine.
    monkeypatch.setattr("compendium.db.engine.get_engine", lambda: engine)
    monkeypatch.setattr("compendium.db.session.get_engine", lambda: engine)
    ss.invalidate_cache()
    yield engine
    ss.invalidate_cache()


def test_settings_reset_prompts_and_aborts_on_no(cli_db):
    result = runner.invoke(app, ["settings", "reset", "default_loan_period_days"], input="n\n")
    assert result.exit_code != 0
    assert "Reset" not in result.output.split("?")[-1]  # no success line after the prompt


def test_settings_reset_yes_skips_prompt(cli_db):
    result = runner.invoke(app, ["settings", "reset", "default_loan_period_days", "--yes"])
    assert result.exit_code == 0
    assert "?" not in result.output  # no prompt rendered


def test_secrets_clear_prompts(cli_db):
    result = runner.invoke(app, ["secrets", "clear", "smtp_password"], input="n\n")
    assert result.exit_code != 0


def test_secrets_clear_yes_skips_prompt(cli_db):
    result = runner.invoke(app, ["secrets", "clear", "smtp_password", "--yes"])
    assert result.exit_code == 0
    assert "?" not in result.output


def test_patron_category_delete_prompts(cli_db):
    runner.invoke(app, ["patron-category", "add", "--code", "tmp", "--name", "Temp"])
    result = runner.invoke(app, ["patron-category", "delete", "tmp"], input="n\n")
    assert result.exit_code != 0
    listed = runner.invoke(app, ["patron-category", "list"])
    assert "tmp" in listed.output  # still there


def test_patron_category_delete_yes_skips_prompt(cli_db):
    runner.invoke(app, ["patron-category", "add", "--code", "tmp2", "--name", "Temp2"])
    result = runner.invoke(app, ["patron-category", "delete", "tmp2", "--yes"])
    assert result.exit_code == 0
    assert "?" not in result.output
    listed = runner.invoke(app, ["patron-category", "list"])
    assert "tmp2" not in listed.output


def test_closed_date_delete_prompts_and_aborts_on_no(cli_db):
    add = runner.invoke(app, ["calendar", "closed-date", "add", "--start", "2030-02-01"])
    assert add.exit_code == 0
    result = runner.invoke(app, ["calendar", "closed-date", "delete", "--id", "1"], input="n\n")
    assert result.exit_code != 0
    listed = runner.invoke(app, ["calendar", "closed-date", "list"])
    assert "2030-02-01" in listed.output  # still there


def test_closed_date_delete_yes(cli_db):
    add = runner.invoke(app, ["calendar", "closed-date", "add", "--start", "2030-01-01"])
    assert add.exit_code == 0
    result = runner.invoke(app, ["calendar", "closed-date", "delete", "--id", "1", "--yes"])
    assert result.exit_code == 0
