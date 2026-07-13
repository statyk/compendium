"""--quiet: no-op runs are fully silent; errors still print."""
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

    Copied from ``tests/integration/test_cli_confirmations.py`` — these tests
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

    monkeypatch.setattr("compendium.db.engine.get_engine", lambda: engine)
    monkeypatch.setattr("compendium.db.session.get_engine", lambda: engine)
    ss.invalidate_cache()
    yield engine
    ss.invalidate_cache()


QUIET_NOOP_COMMANDS = [
    ["maintenance", "expire-holds"],
    ["maintenance", "prune-audit-log", "--older-than-days", "365"],
    ["maintenance", "prune-scan-pairings", "--older-than-days", "365"],
    ["maintenance", "prune-failed-logins", "--older-than-days", "365"],
    ["maintenance", "assess-overdue-fines"],
    ["maintenance", "send-queued-notifications"],
    ["maintenance", "queue-due-soon-notices"],
    ["maintenance", "queue-overdue-notices"],
    ["maintenance", "prune-notifications", "--older-than-days", "365"],
    ["maintenance", "prune-metadata-cache"],
    ["maintenance", "purge-trash"],
    ["maintenance", "prune-cover-cache"],
]


@pytest.mark.parametrize("cmd", QUIET_NOOP_COMMANDS, ids=lambda c: c[1])
def test_quiet_noop_is_silent(cli_db, cmd, monkeypatch, tmp_path):
    monkeypatch.setenv("COMPENDIUM_COVER_CACHE_DIR", str(tmp_path / "covers"))
    result = runner.invoke(app, [*cmd, "--quiet"])
    assert result.exit_code == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("cmd", QUIET_NOOP_COMMANDS, ids=lambda c: c[1])
def test_without_quiet_noop_still_reports(cli_db, cmd, monkeypatch, tmp_path):
    monkeypatch.setenv("COMPENDIUM_COVER_CACHE_DIR", str(tmp_path / "covers"))
    result = runner.invoke(app, cmd)
    assert result.exit_code == 0
    assert result.stdout != ""
