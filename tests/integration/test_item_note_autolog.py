"""Integration: auto-logged system ItemNotes from lifecycle hooks.

Verifies that CatalogService / CirculationService append ``is_system`` notes at
the right lifecycle transitions (condition change, withdraw, declare-lost,
mark-damaged, clear-damage) and that routine circulation (checkout / checkin)
does NOT.
"""
import pytest

from compendium.domain.enums import ItemNoteKind
from compendium.domain.models import Branch, Item, MediaType, Patron, Work
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService


def _catalog(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        hold_repo=SqlHoldRepository(session),
        item_note_repo=SqlItemNoteRepository(session),
    )


def _circulation(session) -> CirculationService:
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        item_note_repo=SqlItemNoteRepository(session),
    )


def _notes(session, item_id):
    return SqlItemNoteRepository(session).list_for_item(item_id)


@pytest.fixture
def item(session):
    branch = session.query(Branch).filter_by(is_default=True).one()
    media = session.query(MediaType).filter_by(code="book").one()
    work = Work(title="Dune", media_type_id=media.id)
    session.add(work)
    session.flush()
    it = Item(
        work_id=work.id,
        branch_id=branch.id,
        barcode="AUTOLOG0001",
        accession_number="ACC-AUTOLOG-0001",
        condition="good",
        status="available",
    )
    session.add(it)
    session.flush()
    return it


@pytest.fixture
def patron(session):
    p = Patron(library_card_number="AUTOLOG-P1", full_name="Auto Log Patron")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


def test_update_item_condition_logs_system_note(session, item):
    _catalog(session).update_item(item.barcode, condition="fair")
    session.flush()

    notes = _notes(session, item.id)
    assert len(notes) == 1
    note = notes[0]
    assert note.is_system is True
    assert note.kind == ItemNoteKind.CONDITION.value
    assert "good" in note.note
    assert "fair" in note.note


def test_update_item_no_condition_change_no_note(session, item):
    # Changing only location must not emit a condition note.
    _catalog(session).update_item(item.barcode, location="Shelf B3")
    session.flush()
    assert _notes(session, item.id) == []


def test_withdraw_item_logs_status_note(session, item):
    _catalog(session).withdraw_item(item.barcode)
    session.flush()

    notes = _notes(session, item.id)
    assert len(notes) == 1
    assert notes[0].is_system is True
    assert notes[0].kind == ItemNoteKind.STATUS.value


def test_declare_lost_logs_status_note(session, item, patron):
    circ = _circulation(session)
    circ.checkout(item.barcode, patron.library_card_number)
    session.flush()
    # checkout itself produced no note
    assert _notes(session, item.id) == []

    circ.declare_lost(item.barcode)
    session.flush()

    notes = _notes(session, item.id)
    assert len(notes) == 1
    assert notes[0].is_system is True
    assert notes[0].kind == ItemNoteKind.STATUS.value


def test_mark_damaged_logs_status_note(session, item):
    _circulation(session).mark_damaged(item.barcode, amount_cents=500, note="water damage")
    session.flush()

    notes = _notes(session, item.id)
    assert len(notes) == 1
    assert notes[0].is_system is True
    assert notes[0].kind == ItemNoteKind.STATUS.value


def test_clear_damage_logs_status_note(session, item):
    circ = _circulation(session)
    circ.mark_damaged(item.barcode, amount_cents=500, note="water damage")
    session.flush()
    circ.clear_damage(item.barcode)
    session.flush()

    notes = _notes(session, item.id)
    # one for mark_damaged, one for clear_damage
    assert len(notes) == 2
    assert all(n.is_system for n in notes)
    assert all(n.kind == ItemNoteKind.STATUS.value for n in notes)


def test_routine_checkout_checkin_logs_no_note(session, item, patron):
    circ = _circulation(session)
    circ.checkout(item.barcode, patron.library_card_number)
    session.flush()
    circ.checkin(item.barcode)
    session.flush()

    assert _notes(session, item.id) == []
