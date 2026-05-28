# tests/integration/test_cli_household.py
"""Integration: CLI household subcommands."""
import pytest
from contextlib import contextmanager
from typer.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from compendium.cli.main import app
from compendium.config.seed import seed_defaults
from compendium.domain.models import Base, Household, Patron
from compendium.repositories.sql.household_repository import SqlHouseholdRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from tests.helpers import setup_sqlite_fts

runner = CliRunner()


@pytest.fixture(scope="module")
def cli_engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    setup_sqlite_fts(e)
    return e


@pytest.fixture
def db(cli_engine):
    factory = sessionmaker(bind=cli_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


def _session_scope_for(db_session):
    """Patch session_scope to yield the test session."""
    @contextmanager
    def _scope():
        yield db_session
    return _scope


def test_household_create(db):
    with patch("compendium.cli.commands.household.session_scope", _session_scope_for(db)):
        result = runner.invoke(app, ["household", "create", "--name", "CLI Test HH"])
    assert result.exit_code == 0, result.output
    assert "CLI Test HH" in result.output


def test_household_list(db):
    SqlHouseholdRepository(db).add(Household(name="Listed HH"))
    db.commit()
    with patch("compendium.cli.commands.household.session_scope", _session_scope_for(db)):
        result = runner.invoke(app, ["household", "list"])
    assert result.exit_code == 0, result.output
    assert "Listed HH" in result.output


def test_household_add_member(db):
    hh = SqlHouseholdRepository(db).add(Household(name="Add Member HH"))
    patron = Patron(library_card_number="CLI-001", full_name="Eve")
    SqlPatronRepository(db).add(patron)
    db.commit()
    with patch("compendium.cli.commands.household.session_scope", _session_scope_for(db)):
        result = runner.invoke(
            app, ["household", "add-member", "--id", str(hh.id), "--card", "CLI-001"]
        )
    assert result.exit_code == 0, result.output
    db.refresh(patron)
    assert patron.household_id == hh.id


def test_household_remove_member(db):
    hh = SqlHouseholdRepository(db).add(Household(name="Remove Member HH"))
    patron = Patron(library_card_number="CLI-002", full_name="Frank")
    SqlPatronRepository(db).add(patron)
    db.commit()
    patron.household_id = hh.id
    SqlPatronRepository(db).update(patron)
    db.commit()
    with patch("compendium.cli.commands.household.session_scope", _session_scope_for(db)):
        result = runner.invoke(
            app, ["household", "remove-member", "--id", str(hh.id), "--card", "CLI-002"]
        )
    assert result.exit_code == 0, result.output
    db.refresh(patron)
    assert patron.household_id is None
