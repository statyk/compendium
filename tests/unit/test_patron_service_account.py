"""Unit tests for PatronService.create_with_account and create_account_for_patron."""
from unittest.mock import MagicMock, patch

import pytest

from compendium.domain.errors import BusinessRuleError, ConflictError, NotFoundError
from compendium.domain.models import AppUser, Patron, Role
from compendium.services.auth import hash_password
from compendium.services.patrons import PatronService


def _patron_role() -> Role:
    r = Role(name="Patron", permissions=["loan.renew.self"], is_system=True)
    r.id = 3
    return r


def _make_user(uid: int = 10) -> AppUser:
    role = _patron_role()
    u = AppUser(username="newpatron", password_hash=hash_password("long-enough-pw"), role_id=role.id)
    u.id = uid
    u.role = role
    u.is_active = True
    return u


def _patron_svc(
    patron_repo=None,
    auth_svc=None,
    loan_repo=None,
    hold_repo=None,
) -> PatronService:
    pr = patron_repo or MagicMock()
    return PatronService(
        patron_repo=pr,
        loan_repo=loan_repo or MagicMock(),
        hold_repo=hold_repo or MagicMock(),
        auth_svc=auth_svc,
    )


def _stub_card_number(patron_repo, card: str = "LIB-0001"):
    """Make patron_repo look like no card is taken."""
    patron_repo.get_by_card_number.return_value = None
    patron_repo.get_by_user_id.return_value = None

    def _add(p: Patron):
        p.id = 99
        p.library_card_number = card
        return p

    patron_repo.add.side_effect = _add


class TestCreateWithAccount:
    def test_raises_when_no_auth_svc(self):
        svc = _patron_svc()
        with pytest.raises(BusinessRuleError, match="not configured"):
            svc.create_with_account(
                full_name="Alice",
                username="alice",
                password="long-enough-pw",
            )

    def test_happy_path_creates_user_and_patron(self):
        patron_repo = MagicMock()
        _stub_card_number(patron_repo)

        auth_svc = MagicMock()
        new_user = _make_user(uid=10)
        auth_svc.create_user.return_value = new_user

        with patch("compendium.services.site_settings.get_site_setting", side_effect=_fake_setting):
            svc = _patron_svc(patron_repo=patron_repo, auth_svc=auth_svc)
            patron = svc.create_with_account(
                full_name="Alice Patron",
                username="alice",
                password="long-enough-pw",
            )

        auth_svc.create_user.assert_called_once_with("alice", "long-enough-pw", "Patron")
        assert patron_repo.add.called
        added: Patron = patron_repo.add.call_args[0][0]
        assert added.user_id == 10
        assert added.full_name == "Alice Patron"

    def test_user_creation_failure_propagates(self):
        patron_repo = MagicMock()
        auth_svc = MagicMock()
        auth_svc.create_user.side_effect = ConflictError("Username taken")

        with patch("compendium.services.site_settings.get_site_setting", side_effect=_fake_setting):
            svc = _patron_svc(patron_repo=patron_repo, auth_svc=auth_svc)
            with pytest.raises(ConflictError, match="Username taken"):
                svc.create_with_account(
                    full_name="Alice",
                    username="taken",
                    password="long-enough-pw",
                )
        patron_repo.add.assert_not_called()


class TestCreateAccountForPatron:
    def _existing_patron(self, uid: int | None = None) -> Patron:
        p = Patron(library_card_number="LIB-0001", full_name="Existing P.")
        p.id = 55
        p.user_id = uid
        p.is_active = True
        return p

    def test_raises_when_no_auth_svc(self):
        patron_repo = MagicMock()
        patron_repo.get_by_card_number.return_value = self._existing_patron()
        svc = _patron_svc(patron_repo=patron_repo)
        with pytest.raises(BusinessRuleError, match="not configured"):
            svc.create_account_for_patron("LIB-0001", username="u", password="pw")

    def test_raises_when_patron_not_found(self):
        patron_repo = MagicMock()
        patron_repo.get_by_card_number.return_value = None
        auth_svc = MagicMock()
        svc = _patron_svc(patron_repo=patron_repo, auth_svc=auth_svc)
        with pytest.raises(NotFoundError):
            svc.create_account_for_patron("BAD-CARD", username="u", password="pw")

    def test_raises_when_patron_already_linked(self):
        patron_repo = MagicMock()
        patron_repo.get_by_card_number.return_value = self._existing_patron(uid=7)
        auth_svc = MagicMock()
        svc = _patron_svc(patron_repo=patron_repo, auth_svc=auth_svc)
        with pytest.raises(BusinessRuleError, match="already has a linked"):
            svc.create_account_for_patron("LIB-0001", username="u", password="long-enough-pw")

    def test_happy_path_links_new_account(self):
        patron_repo = MagicMock()
        existing = self._existing_patron(uid=None)
        patron_repo.get_by_card_number.return_value = existing
        patron_repo.get_by_user_id.return_value = None

        def _update(p):
            return p
        patron_repo.update.side_effect = _update

        auth_svc = MagicMock()
        auth_svc.create_user.return_value = _make_user(uid=20)

        svc = _patron_svc(patron_repo=patron_repo, auth_svc=auth_svc)
        result = svc.create_account_for_patron("LIB-0001", username="newlogin", password="long-enough-pw")

        auth_svc.create_user.assert_called_once_with("newlogin", "long-enough-pw", "Patron")
        assert result.user_id == 20


def _fake_setting(key: str):
    """Minimal stub for get_site_setting so card number generation works."""
    if key == "barcode_location_enabled":
        return False
    if key == "barcode_default_location_code":
        return None
    return None
