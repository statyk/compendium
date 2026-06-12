"""ISBN/UPC circulation fallback (spec: 2026-06-12-isbn-circulation-design).

Checkout/checkin/renew accept a printed ISBN or UPC when the scanned code is
not a Compendium item barcode, gated on `circulation_scan_isbn_enabled`.
"""

from unittest.mock import patch

import pytest

from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.errors import AmbiguousItemError, BusinessRuleError, NotFoundError
from compendium.domain.models import Hold, Item, MediaType, Patron, Work
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.circulation import CirculationService

ISBN13 = "9780441013593"
ISBN10 = "0441013597"  # same book; normalize_isbn() converts to ISBN13
UPC = "043396077478"


def _circulation(session) -> CirculationService:
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        work_repo=SqlWorkRepository(session),
    )


def _setting_off(key):
    if key == "circulation_scan_isbn_enabled":
        return False
    from compendium.services.site_settings import get_site_setting

    return get_site_setting(key)


@pytest.fixture
def work(session):
    book = session.query(MediaType).filter_by(code="book").one()
    w = Work(title="Dune", media_type_id=book.id, isbn=ISBN13)
    SqlWorkRepository(session).add(w)
    session.flush()
    return w


@pytest.fixture
def copies(session, work):
    branch = SqlBranchRepository(session).get_default()
    items = []
    for n in (1, 2):
        i = Item(
            work_id=work.id,
            branch_id=branch.id,
            barcode=f"ISBNTEST-{n}",
            accession_number=f"ISBNTEST-A{n}",
        )
        SqlItemRepository(session).add(i)
        items.append(i)
    session.flush()
    return items


@pytest.fixture
def patron(session):
    p = Patron(library_card_number="ISBN0001", full_name="First Patron")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


@pytest.fixture
def patron2(session):
    p = Patron(library_card_number="ISBN0002", full_name="Second Patron")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


# ── checkout ──────────────────────────────────────────────────────────────────


def test_checkout_by_isbn_picks_lowest_accession(session, copies, patron):
    loan = _circulation(session).checkout(ISBN13, patron.library_card_number)
    assert loan.item_id == copies[0].id
    assert copies[0].status == ItemStatus.CHECKED_OUT


def test_checkout_by_isbn10_normalizes(session, copies, patron):
    loan = _circulation(session).checkout(ISBN10, patron.library_card_number)
    assert loan.item_id == copies[0].id


def test_checkout_exact_barcode_still_wins(session, copies, patron):
    loan = _circulation(session).checkout("ISBNTEST-2", patron.library_card_number)
    assert loan.item_id == copies[1].id


def test_checkout_by_isbn_skips_checked_out_copy(session, copies, patron, patron2):
    _circulation(session).checkout(ISBN13, patron2.library_card_number)
    loan = _circulation(session).checkout(ISBN13, patron.library_card_number)
    assert loan.item_id == copies[1].id


def test_checkout_by_isbn_no_copies_available(session, copies, patron, patron2):
    circ = _circulation(session)
    circ.checkout(ISBN13, patron2.library_card_number)
    circ.checkout(ISBN13, patron2.library_card_number)
    with pytest.raises(BusinessRuleError, match="No available copy"):
        circ.checkout(ISBN13, patron.library_card_number)


def test_checkout_by_isbn_prefers_copy_held_for_patron(session, work, copies, patron):
    branch = SqlBranchRepository(session).get_default()
    hold = Hold(
        work_id=work.id,
        patron_id=patron.id,
        branch_id=branch.id,
        status=HoldStatus.AVAILABLE.value,
        held_item_id=copies[1].id,
    )
    session.add(hold)
    copies[1].status = ItemStatus.ON_HOLD.value
    session.flush()
    loan = _circulation(session).checkout(ISBN13, patron.library_card_number)
    assert loan.item_id == copies[1].id
    assert hold.status == HoldStatus.FULFILLED.value


def test_checkout_unknown_isbn_raises_not_found(session, copies, patron):
    with pytest.raises(NotFoundError, match="ISBN"):
        _circulation(session).checkout("9799999999999", patron.library_card_number)


def test_checkout_alphanumeric_unknown_code_raises_not_found(session, copies, patron):
    # 10-char alphanumeric codes must yield NotFoundError, not a ValueError leak
    # from ISBN-10 normalization (e.g. labels from another library system).
    with pytest.raises(NotFoundError, match="matching"):
        _circulation(session).checkout("MYLIB12345", patron.library_card_number)


def test_checkout_by_isbn_disabled_by_setting(session, copies, patron):
    with patch(
        "compendium.services.circulation.get_site_setting", side_effect=_setting_off
    ):
        with pytest.raises(NotFoundError, match="No item with barcode"):
            _circulation(session).checkout(ISBN13, patron.library_card_number)


def test_checkout_without_work_repo_keeps_old_behavior(session, copies, patron):
    circ = CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )
    with pytest.raises(NotFoundError, match="No item with barcode"):
        circ.checkout(ISBN13, patron.library_card_number)


# ── UPC ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def dvd_copy(session):
    dvd = session.query(MediaType).filter_by(code="dvd").one()
    w = Work(title="Blade Runner", media_type_id=dvd.id, upc=UPC)
    SqlWorkRepository(session).add(w)
    session.flush()
    branch = SqlBranchRepository(session).get_default()
    i = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode="UPCTEST-1",
        accession_number="UPCTEST-A1",
    )
    SqlItemRepository(session).add(i)
    session.flush()
    return i


def test_checkout_by_upc(session, dvd_copy, patron):
    loan = _circulation(session).checkout(UPC, patron.library_card_number)
    assert loan.item_id == dvd_copy.id


def test_checkout_by_ean13_form_of_stored_upc(session, dvd_copy, patron):
    # Scanners often report UPC-A as 13-digit EAN with a leading zero.
    loan = _circulation(session).checkout("0" + UPC, patron.library_card_number)
    assert loan.item_id == dvd_copy.id


# ── checkin ───────────────────────────────────────────────────────────────────


def test_checkin_by_isbn_single_loan(session, copies, patron):
    circ = _circulation(session)
    circ.checkout(ISBN13, patron.library_card_number)
    loan = circ.checkin(ISBN13)
    assert loan.returned_at is not None
    assert loan.item_id == copies[0].id


def test_checkin_by_isbn_nothing_on_loan(session, copies):
    with pytest.raises(BusinessRuleError, match="are checked out"):
        _circulation(session).checkin(ISBN13)


def test_checkin_by_isbn_two_loans_is_ambiguous(session, copies, patron, patron2):
    circ = _circulation(session)
    circ.checkout(ISBN13, patron.library_card_number)
    circ.checkout(ISBN13, patron2.library_card_number)
    with pytest.raises(AmbiguousItemError) as exc_info:
        circ.checkin(ISBN13)
    assert len(exc_info.value.loans) == 2
    assert exc_info.value.work_title == "Dune"


def test_checkin_exact_barcode_never_ambiguous(session, copies, patron, patron2):
    circ = _circulation(session)
    circ.checkout(ISBN13, patron.library_card_number)
    circ.checkout(ISBN13, patron2.library_card_number)
    loan = circ.checkin("ISBNTEST-2")
    assert loan.item_id == copies[1].id
