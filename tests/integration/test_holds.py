"""Integration tests for holds and loan policies (full lifecycle)."""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.errors import BusinessRuleError
from compendium.domain.models import LoanPolicy, Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.holds import HoldService

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}
_ISBN = "9780441013593"


def _catalog(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
    )


def _circulation(session) -> CirculationService:
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=3,
    )


def _holds(session) -> HoldService:
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_expiry_days=30,
    )


@pytest.fixture
def work_and_item(session):
    with patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        work, item = _catalog(session).add_from_isbn(_ISBN)
    session.flush()
    return work, item


@pytest.fixture
def patron(session):
    p = Patron(library_card_number="TEST0001", full_name="Alice")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


@pytest.fixture
def patron2(session):
    p = Patron(library_card_number="TEST0002", full_name="Bob")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


@pytest.fixture
def default_policy(session):
    policy = LoanPolicy(name="Default", loan_period_days=14, max_renewals=2, is_default=True)
    SqlLoanPolicyRepository(session).add(policy)
    session.flush()
    return policy


# ── Hold placement ────────────────────────────────────────────────────────────

def test_place_hold_creates_waiting(session, work_and_item, patron):
    work, _ = work_and_item
    hold = _holds(session).place(work.id, patron.library_card_number)
    assert hold.status == HoldStatus.WAITING.value
    assert hold.work_id == work.id
    assert hold.patron_id == patron.id
    assert hold.expires_at is not None


def test_place_hold_duplicate_raises(session, work_and_item, patron):
    work, _ = work_and_item
    _holds(session).place(work.id, patron.library_card_number)
    with pytest.raises(BusinessRuleError, match="already has an active hold"):
        _holds(session).place(work.id, patron.library_card_number)


def test_cancel_hold(session, work_and_item, patron):
    work, _ = work_and_item
    hold = _holds(session).place(work.id, patron.library_card_number)
    result = _holds(session).cancel(hold.id, patron.id)
    assert result.status == HoldStatus.CANCELLED.value


# ── Checkin promotes hold ─────────────────────────────────────────────────────

def test_checkin_promotes_oldest_waiting_hold(
    session, work_and_item, patron, patron2, default_policy
):
    work, item = work_and_item
    _circulation(session).checkout(item.barcode, patron.library_card_number)
    # patron2 places a hold while item is checked out
    hold = _holds(session).place(work.id, patron2.library_card_number)
    assert hold.status == HoldStatus.WAITING.value

    _circulation(session).checkin(item.barcode)

    # hold should now be AVAILABLE; item should be ON_HOLD
    session.refresh(hold)
    session.refresh(item)
    assert hold.status == HoldStatus.AVAILABLE.value
    assert item.status == ItemStatus.ON_HOLD


def test_checkin_no_hold_frees_item(session, work_and_item, patron, default_policy):
    _, item = work_and_item
    _circulation(session).checkout(item.barcode, patron.library_card_number)
    _circulation(session).checkin(item.barcode)
    session.refresh(item)
    assert item.status == ItemStatus.AVAILABLE


# ── Checkout ON_HOLD item ─────────────────────────────────────────────────────

def test_checkout_on_hold_item_for_hold_patron(
    session, work_and_item, patron, patron2, default_policy
):
    work, item = work_and_item
    # Checkout and checkin to create a promoted hold for patron2
    _circulation(session).checkout(item.barcode, patron.library_card_number)
    _holds(session).place(work.id, patron2.library_card_number)
    _circulation(session).checkin(item.barcode)

    session.refresh(item)
    assert item.status == ItemStatus.ON_HOLD

    loan = _circulation(session).checkout(item.barcode, patron2.library_card_number)
    assert loan.patron_id == patron2.id
    session.refresh(item)
    assert item.status == ItemStatus.CHECKED_OUT


def test_checkout_on_hold_item_wrong_patron_raises(
    session, work_and_item, patron, patron2, default_policy
):
    work, item = work_and_item
    _circulation(session).checkout(item.barcode, patron.library_card_number)
    _holds(session).place(work.id, patron2.library_card_number)
    _circulation(session).checkin(item.barcode)

    session.refresh(item)
    assert item.status == ItemStatus.ON_HOLD

    with pytest.raises(BusinessRuleError, match="reserved for another patron"):
        _circulation(session).checkout(item.barcode, patron.library_card_number)


# ── Loan renewal ──────────────────────────────────────────────────────────────

def test_renew_extends_due_date(session, work_and_item, patron, default_policy):
    _, item = work_and_item
    loan = _circulation(session).checkout(item.barcode, patron.library_card_number)
    original_due = loan.due_at

    renewed = _circulation(session).renew(item.barcode, patron.library_card_number)
    assert renewed.due_at > original_due
    assert renewed.renewal_count == 1


def test_renew_exceeds_limit_raises(session, work_and_item, patron, default_policy):
    _, item = work_and_item
    _circulation(session).checkout(item.barcode, patron.library_card_number)
    # exhaust renewals
    for _ in range(default_policy.max_renewals):
        _circulation(session).renew(item.barcode, patron.library_card_number)
    with pytest.raises(BusinessRuleError, match="renewal limit"):
        _circulation(session).renew(item.barcode, patron.library_card_number)


def test_renew_wrong_patron_raises(session, work_and_item, patron, patron2, default_policy):
    _, item = work_and_item
    _circulation(session).checkout(item.barcode, patron.library_card_number)
    with pytest.raises(BusinessRuleError, match="does not belong"):
        _circulation(session).renew(item.barcode, patron2.library_card_number)


# ── Expire holds ──────────────────────────────────────────────────────────────

def test_expire_holds_marks_expired(session, work_and_item, patron):
    work, _ = work_and_item
    hold = _holds(session).place(work.id, patron.library_card_number)
    # backdate the expiry
    hold.expires_at = datetime.utcnow() - timedelta(days=1)
    session.flush()

    count = _holds(session).expire_holds()
    assert count == 1
    session.refresh(hold)
    assert hold.status == HoldStatus.EXPIRED.value


# ── Loan policy ───────────────────────────────────────────────────────────────

def test_checkout_uses_policy_loan_period(session, work_and_item, patron):
    work, item = work_and_item
    # media-type-specific policy takes priority over the default
    policy = LoanPolicy(
        name="Short",
        media_type_id=work.media_type_id,
        loan_period_days=7,
        max_renewals=1,
        is_default=False,
    )
    SqlLoanPolicyRepository(session).add(policy)
    session.flush()

    loan = _circulation(session).checkout(item.barcode, patron.library_card_number)
    delta = loan.due_at - loan.checked_out_at
    assert delta.days == 7
