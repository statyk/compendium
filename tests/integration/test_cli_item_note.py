# tests/integration/test_cli_item_note.py
"""Integration: CLI item note subcommands."""
import pytest
from contextlib import contextmanager
from typer.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from sqlalchemy import select

from compendium.cli.main import app
from compendium.config.seed import seed_defaults
from compendium.domain.models import Base, Branch, Item, ItemNote, MediaType, Work
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
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


def _make_item(db, barcode: str) -> Item:
    """Create a minimal work + item for testing."""
    media_type = db.execute(select(MediaType).where(MediaType.code == "book")).scalar_one()
    branch = db.execute(select(Branch).where(Branch.is_default == True)).scalar_one()  # noqa: E712
    work = Work(title=f"Test Book {barcode}", media_type_id=media_type.id)
    db.add(work)
    db.flush()
    item = Item(
        work_id=work.id,
        branch_id=branch.id,
        barcode=barcode,
        accession_number=f"ACC-{barcode}",
    )
    db.add(item)
    db.commit()
    return item


def test_note_add_exits_zero(db):
    """Adding a note to an existing item exits 0 and reports success."""
    item = _make_item(db, "NOTE-001")
    with patch("compendium.cli.commands.item.session_scope", _session_scope_for(db)):
        result = runner.invoke(
            app,
            ["item", "note", "add", "NOTE-001", "--note", "Slight tear on cover", "--kind", "repair"],
        )
    assert result.exit_code == 0, result.output
    assert "Note added" in result.output


def test_note_list_shows_note(db):
    """Listing notes for an item shows the note text."""
    item = _make_item(db, "NOTE-002")
    # Add note directly via repo
    note_repo = SqlItemNoteRepository(db)
    note_repo.add(ItemNote(item_id=item.id, kind="general", note="A direct note", is_system=False))
    db.commit()

    with patch("compendium.cli.commands.item.session_scope", _session_scope_for(db)):
        result = runner.invoke(app, ["item", "note", "list", "NOTE-002"])
    assert result.exit_code == 0, result.output
    assert "A direct note" in result.output


def test_note_delete_exits_zero(db):
    """Deleting a non-system note with --yes exits 0 and reports deletion."""
    item = _make_item(db, "NOTE-003")
    note_repo = SqlItemNoteRepository(db)
    note = note_repo.add(ItemNote(item_id=item.id, kind="general", note="Delete me", is_system=False))
    db.commit()
    note_id = note.id

    with patch("compendium.cli.commands.item.session_scope", _session_scope_for(db)):
        result = runner.invoke(
            app, ["item", "note", "delete", "NOTE-003", str(note_id), "--yes"]
        )
    assert result.exit_code == 0, result.output
    assert "deleted" in result.output.lower()

    # Confirm it's gone
    db.expire_all()
    assert note_repo.get(note_id) is None


def test_delete_system_note_exits_one(db):
    """Attempting to delete a system note exits 1 with an error message."""
    item = _make_item(db, "NOTE-004")
    note_repo = SqlItemNoteRepository(db)
    sys_note = note_repo.add(ItemNote(item_id=item.id, kind="status", note="System note", is_system=True))
    db.commit()
    note_id = sys_note.id

    with patch("compendium.cli.commands.item.session_scope", _session_scope_for(db)):
        result = runner.invoke(
            app, ["item", "note", "delete", "NOTE-004", str(note_id), "--yes"]
        )
    assert result.exit_code == 1
    assert "Error" in result.output or "error" in result.output.lower()


def test_note_add_nonexistent_barcode_exits_one(db):
    """Adding a note to a non-existent barcode exits 1."""
    with patch("compendium.cli.commands.item.session_scope", _session_scope_for(db)):
        result = runner.invoke(
            app,
            ["item", "note", "add", "DOES-NOT-EXIST", "--note", "test", "--kind", "general"],
        )
    assert result.exit_code == 1
    assert "Error" in result.output or "error" in result.output.lower()


def test_note_list_no_notes(db):
    """Listing notes for an item with no notes prints a helpful message."""
    _make_item(db, "NOTE-005")
    with patch("compendium.cli.commands.item.session_scope", _session_scope_for(db)):
        result = runner.invoke(app, ["item", "note", "list", "NOTE-005"])
    assert result.exit_code == 0, result.output
    assert "No notes" in result.output


def test_note_list_nonexistent_barcode_exits_one(db):
    """Listing notes for a nonexistent barcode exits 1."""
    with patch("compendium.cli.commands.item.session_scope", _session_scope_for(db)):
        result = runner.invoke(app, ["item", "note", "list", "DOES-NOT-EXIST"])
    assert result.exit_code == 1
