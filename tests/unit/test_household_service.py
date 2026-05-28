# tests/unit/test_household_service.py
"""Unit: HouseholdService business logic with mock repos."""
import pytest
from unittest.mock import MagicMock

from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import Household, Patron
from compendium.services.households import HouseholdService


def _svc(**overrides):
    defaults = dict(
        household_repo=MagicMock(),
        patron_repo=MagicMock(),
        loan_repo=MagicMock(),
    )
    defaults.update(overrides)
    return HouseholdService(**defaults)


class TestCreate:
    def test_creates_household(self):
        svc = _svc()
        saved = Household(id=1, name="Smith Family", notes=None)
        svc._households.add.return_value = saved
        result = svc.create(name="Smith Family")
        svc._households.add.assert_called_once()
        assert result.name == "Smith Family"

    def test_blank_name_raises_validation_error(self):
        svc = _svc()
        with pytest.raises(ValidationError, match="name"):
            svc.create(name="   ")


class TestGet:
    def test_returns_household(self):
        svc = _svc()
        hh = Household(id=5, name="Jones")
        svc._households.get.return_value = hh
        assert svc.get(5) is hh

    def test_not_found_raises(self):
        svc = _svc()
        svc._households.get.return_value = None
        with pytest.raises(NotFoundError):
            svc.get(999)


class TestUpdate:
    def test_updates_name(self):
        svc = _svc()
        hh = Household(id=1, name="Old", notes=None)
        svc._households.get.return_value = hh
        svc._households.update.return_value = hh
        svc.update(1, name="New Name")
        assert hh.name == "New Name"

    def test_updates_notes(self):
        svc = _svc()
        hh = Household(id=1, name="Test", notes=None)
        svc._households.get.return_value = hh
        svc._households.update.return_value = hh
        svc.update(1, notes="Family of four")
        assert hh.notes == "Family of four"

    def test_missing_leaves_name_unchanged(self):
        from compendium.services.households import _MISSING
        svc = _svc()
        hh = Household(id=1, name="Keep This", notes=None)
        svc._households.get.return_value = hh
        svc._households.update.return_value = hh
        svc.update(1, name=_MISSING)
        assert hh.name == "Keep This"

    def test_blank_name_raises(self):
        svc = _svc()
        svc._households.get.return_value = Household(id=1, name="X", notes=None)
        with pytest.raises(ValidationError, match="name"):
            svc.update(1, name="  ")


class TestDelete:
    def test_deletes_empty_household(self):
        svc = _svc()
        hh = Household(id=1, name="Empty")
        svc._households.get.return_value = hh
        svc._patrons.list_by_household.return_value = []
        svc.delete(1)
        svc._households.delete.assert_called_once_with(hh)

    def test_raises_if_has_members(self):
        svc = _svc()
        hh = Household(id=1, name="Full")
        svc._households.get.return_value = hh
        member = Patron(id=5, library_card_number="X001", full_name="Alice", household_id=1)
        svc._patrons.list_by_household.return_value = [member]
        with pytest.raises(BusinessRuleError, match="members"):
            svc.delete(1)


class TestAddMember:
    def test_links_patron_to_household(self):
        svc = _svc()
        hh = Household(id=1, name="Test HH")
        patron = Patron(id=5, library_card_number="C001", full_name="Alice", household_id=None)
        updated = Patron(id=5, library_card_number="C001", full_name="Alice", household_id=1)
        svc._households.get.return_value = hh
        svc._patrons.get_by_card_number.return_value = patron
        svc._patrons.update.return_value = updated
        result = svc.add_member(1, "C001")
        assert patron.household_id == 1
        svc._patrons.update.assert_called_once_with(patron)

    def test_patron_not_found_raises(self):
        svc = _svc()
        svc._households.get.return_value = Household(id=1, name="X")
        svc._patrons.get_by_card_number.return_value = None
        with pytest.raises(NotFoundError, match="Patron"):
            svc.add_member(1, "NOTEXIST")

    def test_household_not_found_raises(self):
        svc = _svc()
        svc._households.get.return_value = None
        with pytest.raises(NotFoundError, match="Household"):
            svc.add_member(99, "CARD001")

    def test_already_in_this_household_is_noop(self):
        svc = _svc()
        hh = Household(id=1, name="Test")
        patron = Patron(id=5, library_card_number="C001", full_name="Alice", household_id=1)
        svc._households.get.return_value = hh
        svc._patrons.get_by_card_number.return_value = patron
        svc._patrons.update.return_value = patron
        result = svc.add_member(1, "C001")
        svc._patrons.update.assert_not_called()

    def test_already_in_different_household_raises(self):
        svc = _svc()
        svc._households.get.return_value = Household(id=1, name="HH1")
        patron = Patron(id=5, library_card_number="C001", full_name="Alice", household_id=2)
        svc._patrons.get_by_card_number.return_value = patron
        with pytest.raises(BusinessRuleError, match="already.*household"):
            svc.add_member(1, "C001")


class TestRemoveMember:
    def test_unlinks_patron(self):
        svc = _svc()
        hh = Household(id=1, name="Test")
        patron = Patron(id=5, library_card_number="C001", full_name="Alice", household_id=1)
        updated = Patron(id=5, library_card_number="C001", full_name="Alice", household_id=None)
        svc._households.get.return_value = hh
        svc._patrons.get_by_card_number.return_value = patron
        svc._patrons.update.return_value = updated
        svc.remove_member(1, "C001")
        assert patron.household_id is None
        svc._patrons.update.assert_called_once_with(patron)

    def test_patron_not_in_household_raises(self):
        svc = _svc()
        svc._households.get.return_value = Household(id=1, name="Test")
        patron = Patron(id=5, library_card_number="C001", full_name="Alice", household_id=None)
        svc._patrons.get_by_card_number.return_value = patron
        with pytest.raises(BusinessRuleError, match="not a member"):
            svc.remove_member(1, "C001")
