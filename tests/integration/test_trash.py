"""Integration tests for recoverable work deletion (trash)."""
from datetime import datetime, timedelta, timezone

from compendium.config.seed import _LIBRARIAN_PERMISSIONS
from compendium.domain.models import (
    Branch,
    Creator,
    CuratedList,
    CuratedListEntry,
    DeletedEntity,
    Fine,
    Hold,
    Item,
    ItemNote,
    Loan,
    MediaType,
    Notification,
    Patron,
    ScanEvent,
    ScanPairing,
    Work,
    WorkCreator,
)
from compendium.repositories.sql.trash_repository import SqlTrashRepository


def _mk_work(session, *, title="Dune", isbn="9780441013593", n_items=2):
    """Work with n items, one returned loan + one note on item 0."""
    branch = session.query(Branch).first()
    media = session.query(MediaType).filter_by(code="book").first()
    creator = Creator(display_name="Frank Herbert", sort_name="Herbert, Frank")
    work = Work(title=title, media_type_id=media.id, isbn=isbn, search_text=title)
    work.creators.append(WorkCreator(creator=creator, role="author", display_order=0))
    session.add(work)
    session.flush()
    items = []
    for i in range(n_items):
        item = Item(
            work_id=work.id, branch_id=branch.id,
            barcode=f"BC-{work.id}-{i}", accession_number=f"ACC-{work.id}-{i}",
        )
        session.add(item)
        items.append(item)
    session.flush()
    patron = Patron(library_card_number=f"CARD-{work.id}", full_name="Pat Ron")
    session.add(patron)
    session.flush()
    loan = Loan(
        item_id=items[0].id, patron_id=patron.id, branch_id=branch.id,
        due_at=datetime.now(timezone.utc) - timedelta(days=30),
        returned_at=datetime.now(timezone.utc) - timedelta(days=25),
    )
    note = ItemNote(item_id=items[0].id, note="scuffed cover")
    session.add_all([loan, note])
    session.flush()
    return work, items, patron, loan


def test_deleted_entity_round_trip(session):
    row = DeletedEntity(
        entity_type="work",
        entity_id=42,
        label="Dune — 2 copies",
        payload={"version": 1, "work": {"title": "Dune"}},
    )
    session.add(row)
    session.flush()

    got = session.get(DeletedEntity, row.id)
    assert got.entity_type == "work"
    assert got.payload["work"]["title"] == "Dune"
    assert got.deleted_at is not None
    assert got.deleted_by is None


def test_librarian_preset_includes_work_delete():
    assert "work.delete" in _LIBRARIAN_PERMISSIONS


def test_trash_row_crud_and_retention(session):
    repo = SqlTrashRepository(session)
    old = repo.add(DeletedEntity(entity_type="work", entity_id=1, label="old", payload={}))
    old.deleted_at = datetime.now(timezone.utc) - timedelta(days=100)
    new = repo.add(DeletedEntity(entity_type="work", entity_id=2, label="new", payload={}))
    session.flush()

    assert repo.get(old.id) is old
    listed = repo.list(entity_type="work", limit=10)
    assert [r.id for r in listed] == [new.id, old.id]  # newest first

    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    assert repo.delete_older_than("work", cutoff) == 1
    assert repo.get(old.id) is None
    repo.delete(new)
    assert repo.get(new.id) is None


def test_blocker_counts(session):
    work, items, patron, loan = _mk_work(session)
    repo = SqlTrashRepository(session)
    assert repo.count_active_loans(work.id) == 0
    assert repo.count_outstanding_fines(work.id) == 0

    branch = session.query(Branch).first()
    active = Loan(item_id=items[1].id, patron_id=patron.id, branch_id=branch.id,
                  due_at=datetime.now(timezone.utc) + timedelta(days=14))
    fine = Fine(patron_id=patron.id, loan_id=loan.id, kind="overdue",
                amount_cents=250, status="outstanding")
    session.add_all([active, fine])
    session.flush()
    assert repo.count_active_loans(work.id) == 1
    assert repo.count_outstanding_fines(work.id) == 1

    fine.status = "paid"
    session.flush()
    assert repo.count_outstanding_fines(work.id) == 0
