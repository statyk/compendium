"""Unit tests for withdraw/deactivate operations."""

from unittest.mock import MagicMock

import pytest

from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser, Hold, Item, Loan, Patron
from compendium.services.auth import AuthService
from compendium.services.catalog import CatalogService
from compendium.services.patrons import PatronService

# ── CatalogService.withdraw_item ─────────────────────────────────────────────


def _catalog(item_repo=None):
    item_repo = item_repo or MagicMock()
    return CatalogService(
        work_repo=MagicMock(),
        item_repo=item_repo,
        creator_repo=MagicMock(),
        branch_repo=MagicMock(),
    )


def _item(status=ItemStatus.AVAILABLE, barcode="BC001"):
    return Item(
        id=1, barcode=barcode, work_id=1, branch_id=1, accession_number=barcode, status=status.value
    )


def test_withdraw_available_item():
    item = _item(ItemStatus.AVAILABLE)
    item_repo = MagicMock()
    item_repo.get_by_barcode.return_value = item
    item_repo.update.side_effect = lambda i: i
    result = _catalog(item_repo).withdraw_item("BC001")
    assert result.status == ItemStatus.WITHDRAWN.value


def test_withdraw_checked_out_raises():
    item = _item(ItemStatus.CHECKED_OUT)
    item_repo = MagicMock()
    item_repo.get_by_barcode.return_value = item
    with pytest.raises(BusinessRuleError, match="cannot be withdrawn"):
        _catalog(item_repo).withdraw_item("BC001")


def test_withdraw_on_hold_raises():
    item = _item(ItemStatus.ON_HOLD)
    item_repo = MagicMock()
    item_repo.get_by_barcode.return_value = item
    with pytest.raises(BusinessRuleError, match="cannot be withdrawn"):
        _catalog(item_repo).withdraw_item("BC001")


def test_withdraw_unknown_barcode_raises():
    item_repo = MagicMock()
    item_repo.get_by_barcode.return_value = None
    with pytest.raises(NotFoundError):
        _catalog(item_repo).withdraw_item("NOTREAL")


# ── PatronService.deactivate ─────────────────────────────────────────────────


def _patron_svc(patron_repo=None, loan_repo=None, hold_repo=None):
    return PatronService(
        patron_repo=patron_repo or MagicMock(),
        loan_repo=loan_repo or MagicMock(),
        hold_repo=hold_repo or MagicMock(),
    )


def _patron(is_active=True):
    return Patron(id=1, library_card_number="C001", full_name="Alice", is_active=is_active)


def test_deactivate_patron_no_loans():
    patron = _patron()
    patron_repo = MagicMock()
    patron_repo.get_by_card_number.return_value = patron
    patron_repo.update.side_effect = lambda p: p
    loan_repo = MagicMock()
    loan_repo.get_active_for_patron.return_value = []
    hold_repo = MagicMock()
    hold_repo.get_active_for_patron.return_value = []

    result = _patron_svc(patron_repo, loan_repo, hold_repo).deactivate("C001")
    assert not result.is_active


def test_deactivate_patron_cancels_holds():
    patron = _patron()
    hold = Hold(id=1, work_id=1, patron_id=1, branch_id=1, status=HoldStatus.WAITING.value)
    patron_repo = MagicMock()
    patron_repo.get_by_card_number.return_value = patron
    patron_repo.update.side_effect = lambda p: p
    loan_repo = MagicMock()
    loan_repo.get_active_for_patron.return_value = []
    hold_repo = MagicMock()
    hold_repo.get_active_for_patron.return_value = [hold]
    hold_repo.update.side_effect = lambda h: h

    _patron_svc(patron_repo, loan_repo, hold_repo).deactivate("C001")
    assert hold.status == HoldStatus.CANCELLED.value


def test_deactivate_patron_with_active_loans_raises():
    patron = _patron()
    patron_repo = MagicMock()
    patron_repo.get_by_card_number.return_value = patron
    loan_repo = MagicMock()
    loan_repo.get_active_for_patron.return_value = [Loan(id=1, item_id=1, patron_id=1, branch_id=1)]

    with pytest.raises(BusinessRuleError, match="active loan"):
        _patron_svc(patron_repo, loan_repo).deactivate("C001")


def test_deactivate_already_inactive_patron_raises():
    patron_repo = MagicMock()
    patron_repo.get_by_card_number.return_value = _patron(is_active=False)
    with pytest.raises(BusinessRuleError, match="already inactive"):
        _patron_svc(patron_repo).deactivate("C001")


def test_deactivate_unknown_patron_raises():
    patron_repo = MagicMock()
    patron_repo.get_by_card_number.return_value = None
    with pytest.raises(NotFoundError):
        _patron_svc(patron_repo).deactivate("BADCARD")


# ── AuthService.deactivate_user ───────────────────────────────────────────────


def _auth_svc(user_repo=None):
    from compendium.config.settings import Settings

    return AuthService(
        user_repo=user_repo or MagicMock(),
        role_repo=MagicMock(),
        settings=Settings(jwt_secret_key="insecure-default-change-in-production"),
    )


def _user(is_active=True):
    return AppUser(id=1, username="alice", password_hash="x", role_id=1, is_active=is_active)


def test_deactivate_user():
    user = _user()
    user_repo = MagicMock()
    user_repo.get_by_username.return_value = user
    user_repo.update.side_effect = lambda u: u
    result = _auth_svc(user_repo).deactivate_user("alice")
    assert not result.is_active


def test_deactivate_already_inactive_user_raises():
    user_repo = MagicMock()
    user_repo.get_by_username.return_value = _user(is_active=False)
    with pytest.raises(BusinessRuleError, match="already inactive"):
        _auth_svc(user_repo).deactivate_user("alice")


def test_deactivate_unknown_user_raises():
    user_repo = MagicMock()
    user_repo.get_by_username.return_value = None
    with pytest.raises(NotFoundError):
        _auth_svc(user_repo).deactivate_user("ghost")
