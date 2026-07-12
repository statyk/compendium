"""Integration tests for recoverable work deletion (trash)."""
from datetime import datetime, timedelta, timezone

import pytest

from compendium.config.seed import _LIBRARIAN_PERMISSIONS
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import (
    AuditLog,
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
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.trash_repository import SqlTrashRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.trash import TrashService


def _svc(session) -> TrashService:
    return TrashService(
        trash_repo=SqlTrashRepository(session),
        work_repo=SqlWorkRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
    )


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


def test_delete_work_snapshots_and_removes_graph(session):
    work, items, patron, loan = _mk_work(session, title="Deletable", isbn="9780000000001")
    branch = session.query(Branch).first()
    hold = Hold(work_id=work.id, patron_id=patron.id, branch_id=branch.id, status="waiting")
    fine = Fine(patron_id=patron.id, loan_id=loan.id, item_id=items[0].id,
                kind="overdue", amount_cents=100, status="paid")
    notif = Notification(template_key="overdue", subject="s", body="b",
                         status="sent", loan_id=loan.id)
    cl = CuratedList(slug="staff-picks", name="Staff Picks")
    session.add_all([hold, fine, notif, cl])
    session.flush()
    session.add(CuratedListEntry(list_id=cl.id, work_id=work.id, display_order=1,
                                 annotation="a classic"))
    session.flush()
    work_id, item_ids = work.id, [i.id for i in items]

    summary = _svc(session).delete_work(work_id)

    assert summary.label == "Deletable — 2 copies"
    assert summary.original_work_id == work_id
    assert summary.item_count == 2

    # live rows gone
    assert session.get(Work, work_id) is None
    assert session.query(Item).filter(Item.work_id == work_id).count() == 0
    assert session.query(Loan).filter(Loan.item_id.in_(item_ids)).count() == 0
    assert session.query(Hold).filter(Hold.work_id == work_id).count() == 0
    assert session.query(ItemNote).filter(ItemNote.item_id.in_(item_ids)).count() == 0
    assert session.query(CuratedListEntry).filter_by(work_id=work_id).count() == 0

    # survivors got SET NULL
    session.refresh(fine)
    assert fine.loan_id is None and fine.item_id is None
    session.refresh(notif)
    assert notif.loan_id is None

    # snapshot payload complete; waiting hold captured as cancelled
    row = session.get(DeletedEntity, summary.trash_id)
    p = row.payload
    assert p["version"] == 1
    assert p["work"]["title"] == "Deletable"
    assert len(p["items"]) == 2 and len(p["loans"]) == 1
    assert p["holds"][0]["status"] == "cancelled"
    assert p["item_notes"][0]["note"] == "scuffed cover"
    assert p["creators"] == [{"display_name": "Frank Herbert",
                              "sort_name": "Herbert, Frank",
                              "role": "author", "display_order": 0}]
    assert p["curated_lists"] == [{"slug": "staff-picks", "annotation": "a classic",
                                   "display_order": 1}]

    audit = session.query(AuditLog).filter_by(action="delete", entity_type="work").all()
    assert audit and audit[-1].details["trash_id"] == summary.trash_id


def test_delete_work_blocked_on_active_loan(session):
    work, items, patron, _ = _mk_work(session, isbn="9780000000002")
    branch = session.query(Branch).first()
    session.add(Loan(item_id=items[0].id, patron_id=patron.id, branch_id=branch.id,
                     due_at=datetime.now(timezone.utc) + timedelta(days=7)))
    session.flush()
    with pytest.raises(BusinessRuleError, match="active loan"):
        _svc(session).delete_work(work.id)
    assert session.get(Work, work.id) is not None


def test_delete_work_blocked_on_outstanding_fine(session):
    work, items, patron, loan = _mk_work(session, isbn="9780000000003")
    session.add(Fine(patron_id=patron.id, loan_id=loan.id, kind="overdue",
                     amount_cents=500, status="outstanding"))
    session.flush()
    with pytest.raises(BusinessRuleError, match="outstanding fine"):
        _svc(session).delete_work(work.id)


def test_delete_missing_work_raises(session):
    with pytest.raises(NotFoundError):
        _svc(session).delete_work(999_999)
