from unittest.mock import patch

import pytest

from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService

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
        media_type_repo=SqlMediaTypeRepository(session),
    )


def _circulation(session) -> CirculationService:
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )


@pytest.fixture
def item(session):
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        _, item = _catalog(session).add_from_isbn(_ISBN)
    session.flush()
    return item


@pytest.fixture
def patron(session):
    p = Patron(library_card_number="TEST0001", full_name="Test Patron")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


def test_checkout_marks_item_checked_out(session, item, patron):
    loan = _circulation(session).checkout(item.barcode, patron.library_card_number)

    assert loan.item_id == item.id
    assert loan.patron_id == patron.id
    assert loan.returned_at is None
    assert item.status == "checked_out"


def test_checkout_sets_due_date(session, item, patron):
    loan = _circulation(session).checkout(item.barcode, patron.library_card_number)
    delta = loan.due_at - loan.checked_out_at
    assert delta.days == 14


def test_checkout_unavailable_item_raises(session, item, patron):
    _circulation(session).checkout(item.barcode, patron.library_card_number)
    with pytest.raises(BusinessRuleError, match="not available"):
        _circulation(session).checkout(item.barcode, patron.library_card_number)


def test_checkin_clears_loan(session, item, patron):
    _circulation(session).checkout(item.barcode, patron.library_card_number)
    loan = _circulation(session).checkin(item.barcode)

    assert loan.returned_at is not None
    assert item.status == "available"


def test_checkin_without_loan_raises(session, item):
    with pytest.raises(BusinessRuleError, match="no active loan"):
        _circulation(session).checkin(item.barcode)


def test_checkout_unknown_barcode_raises(session, patron):
    with pytest.raises(NotFoundError):
        _circulation(session).checkout("NOTREAL", patron.library_card_number)


def test_checkout_unknown_patron_raises(session, item):
    with pytest.raises(NotFoundError):
        _circulation(session).checkout(item.barcode, "NOTREAL")


def test_checkout_non_loanable_item_raises(session, item, patron):
    item.is_loanable = False
    item.loan_restriction_reason = "reference"
    session.flush()
    with pytest.raises(BusinessRuleError, match="not loanable"):
        _circulation(session).checkout(item.barcode, patron.library_card_number)
