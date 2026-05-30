# tests/unit/test_item_note_repository.py
"""Unit: SqlItemNoteRepository."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.domain.enums import ItemNoteKind
from compendium.domain.models import Base, Branch, Item, ItemNote, MediaType, Work
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from tests.helpers import setup_sqlite_fts


@pytest.fixture(scope="module")
def engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    setup_sqlite_fts(e)

    # Seed the reference data required for Work + Item
    from compendium.config.seed import seed_defaults

    factory = sessionmaker(bind=e, autoflush=False)
    with factory() as s:
        seed_defaults(s)
        s.commit()

    return e


@pytest.fixture
def session(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.rollback()
    s.close()


def _make_item(session: Session, barcode: str, accession: str) -> Item:
    """Create and flush a minimal Work + Item, returning the Item."""
    media_type = session.execute(
        select(MediaType).where(MediaType.code == "book")
    ).scalar_one()
    branch = session.execute(
        select(Branch).where(Branch.is_default == True)  # noqa: E712
    ).scalar_one()
    work = Work(title="Test Work for Notes", media_type_id=media_type.id)
    session.add(work)
    session.flush()
    item = Item(
        work_id=work.id,
        branch_id=branch.id,
        barcode=barcode,
        accession_number=accession,
    )
    session.add(item)
    session.flush()
    return item


class TestSqlItemNoteRepository:
    def test_add_and_get(self, session):
        repo = SqlItemNoteRepository(session)
        item = _make_item(session, "NOTE-BC-001", "NOTE-ACC-001")
        note = ItemNote(item_id=item.id, note="First note", kind=ItemNoteKind.GENERAL)
        saved = repo.add(note)
        assert saved.id is not None
        fetched = repo.get(saved.id)
        assert fetched is not None
        assert fetched.note == "First note"
        assert fetched.kind == ItemNoteKind.GENERAL.value

    def test_get_missing_returns_none(self, session):
        repo = SqlItemNoteRepository(session)
        assert repo.get(99999) is None

    def test_list_for_item(self, session):
        repo = SqlItemNoteRepository(session)
        item_a = _make_item(session, "NOTE-BC-002", "NOTE-ACC-002")
        item_b = _make_item(session, "NOTE-BC-003", "NOTE-ACC-003")

        n1 = repo.add(ItemNote(item_id=item_a.id, note="Note A1"))
        n2 = repo.add(ItemNote(item_id=item_a.id, note="Note A2"))
        repo.add(ItemNote(item_id=item_b.id, note="Note B1"))

        results_a = repo.list_for_item(item_a.id)
        results_b = repo.list_for_item(item_b.id)

        assert len(results_a) == 2
        assert len(results_b) == 1
        note_texts_a = {n.note for n in results_a}
        assert "Note A1" in note_texts_a
        assert "Note A2" in note_texts_a
        assert results_b[0].note == "Note B1"

    def test_list_ordering_newest_first(self, session):
        repo = SqlItemNoteRepository(session)
        item = _make_item(session, "NOTE-BC-004", "NOTE-ACC-004")

        t_old = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t_new = datetime(2024, 1, 1, 12, 0, 1, tzinfo=timezone.utc)

        older = ItemNote(item_id=item.id, note="Older note", created_at=t_old)
        newer = ItemNote(item_id=item.id, note="Newer note", created_at=t_new)
        session.add_all([older, newer])
        session.flush()

        results = repo.list_for_item(item.id)
        assert len(results) == 2
        assert results[0].note == "Newer note"
        assert results[1].note == "Older note"

    def test_delete(self, session):
        repo = SqlItemNoteRepository(session)
        item = _make_item(session, "NOTE-BC-005", "NOTE-ACC-005")
        note = repo.add(ItemNote(item_id=item.id, note="To be deleted"))
        note_id = note.id
        repo.delete(note)
        assert repo.get(note_id) is None
