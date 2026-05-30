"""Integration: ItemNote model persistence and cascade behaviour."""
import pytest
from datetime import date, datetime, timezone
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.config.seed import seed_defaults
from compendium.domain.enums import ItemNoteKind
from compendium.domain.models import Base, Branch, Item, ItemNote, MediaType, Work
from tests.helpers import setup_sqlite_fts


@pytest.fixture(scope="module")
def note_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)
    return engine


@pytest.fixture
def db(note_engine):
    factory = sessionmaker(bind=note_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


def _make_item(db: Session) -> Item:
    """Create and flush a minimal work + item, returning the item."""
    media_type = db.execute(select(MediaType).where(MediaType.code == "book")).scalar_one()
    branch = db.execute(select(Branch).where(Branch.is_default == True)).scalar_one()  # noqa: E712
    work = Work(
        title="Test Work for Notes",
        media_type_id=media_type.id,
    )
    db.add(work)
    db.flush()
    item = Item(
        work_id=work.id,
        branch_id=branch.id,
        barcode="NOTE-BARCODE-001",
        accession_number="NOTE-ACC-001",
    )
    db.add(item)
    db.flush()
    return item


def test_item_note_attached_to_item(db):
    """Two ItemNotes attached to an item appear on note_entries."""
    item = _make_item(db)

    note1 = ItemNote(item_id=item.id, note="First note", kind=ItemNoteKind.GENERAL)
    note2 = ItemNote(item_id=item.id, note="Condition note", kind=ItemNoteKind.CONDITION)
    db.add_all([note1, note2])
    db.flush()

    db.refresh(item)
    assert len(item.note_entries) == 2
    texts = {n.note for n in item.note_entries}
    assert "First note" in texts
    assert "Condition note" in texts


def test_item_note_order_newest_first(db):
    """note_entries is ordered by created_at descending (newest first).

    We supply explicit timestamps that are one second apart to avoid SQLite's
    limited sub-second resolution producing identical values in rapid succession.
    """
    item = _make_item(db)

    t_old = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_new = datetime(2024, 1, 1, 12, 0, 1, tzinfo=timezone.utc)

    older = ItemNote(item_id=item.id, note="Older note", created_at=t_old)
    newer = ItemNote(item_id=item.id, note="Newer note", created_at=t_new)
    db.add_all([older, newer])
    db.flush()

    db.refresh(item)
    # newest first
    assert item.note_entries[0].note == "Newer note"
    assert item.note_entries[1].note == "Older note"


def test_item_note_event_date_nullable(db):
    """event_date is nullable."""
    item = _make_item(db)

    note = ItemNote(item_id=item.id, note="No date note")
    db.add(note)
    db.flush()
    assert note.event_date is None

    note_with_date = ItemNote(
        item_id=item.id,
        note="Backdated note",
        event_date=date(2020, 1, 1),
    )
    db.add(note_with_date)
    db.flush()
    assert note_with_date.event_date == date(2020, 1, 1)


def test_item_note_is_system_defaults_false(db):
    """is_system defaults to False for manually-created notes."""
    item = _make_item(db)
    note = ItemNote(item_id=item.id, note="Manual note")
    db.add(note)
    db.flush()
    assert note.is_system is False


def test_item_delete_cascades_to_notes(db):
    """Deleting an Item deletes its ItemNotes (CASCADE)."""
    item = _make_item(db)
    note = ItemNote(item_id=item.id, note="Will be deleted")
    db.add(note)
    db.flush()
    note_id = note.id

    db.delete(item)
    db.flush()

    surviving = db.get(ItemNote, note_id)
    assert surviving is None
