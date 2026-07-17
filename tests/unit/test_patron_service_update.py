"""Unit tests for PatronService.update's full_name guard.

_MISSING sentinel means "leave untouched"; an explicit full_name value should
always be a real name. Before this fix, an explicit full_name=None slipped
past the blank-check (`str(None).strip()` == "None", which is truthy) and
silently corrupted the patron's name to the literal string "None".
"""
from unittest.mock import MagicMock

import pytest

from compendium.domain.errors import ValidationError
from compendium.domain.models import Patron
from compendium.services.patrons import PatronService


def _patron_svc(patron_repo=None) -> PatronService:
    pr = patron_repo or MagicMock()
    return PatronService(
        patron_repo=pr,
        loan_repo=MagicMock(),
        hold_repo=MagicMock(),
    )


def _existing_patron() -> Patron:
    p = Patron(library_card_number="C-1", full_name="Original Name")
    p.id = 1
    return p


def test_update_full_name_none_is_rejected_not_corrupted():
    repo = MagicMock()
    repo.get_by_card_number.return_value = _existing_patron()
    svc = _patron_svc(repo)

    with pytest.raises(ValidationError):
        svc.update("C-1", full_name=None)

    repo.update.assert_not_called()


def test_update_full_name_empty_string_is_rejected():
    repo = MagicMock()
    repo.get_by_card_number.return_value = _existing_patron()
    svc = _patron_svc(repo)

    with pytest.raises(ValidationError):
        svc.update("C-1", full_name="   ")

    repo.update.assert_not_called()


def test_update_full_name_omitted_leaves_name_untouched():
    repo = MagicMock()
    patron = _existing_patron()
    repo.get_by_card_number.return_value = patron
    repo.update.side_effect = lambda p: p
    svc = _patron_svc(repo)

    result = svc.update("C-1", contact_email="new@example.com")

    assert result.full_name == "Original Name"


def test_update_full_name_valid_value_still_applies():
    repo = MagicMock()
    patron = _existing_patron()
    repo.get_by_card_number.return_value = patron
    repo.update.side_effect = lambda p: p
    svc = _patron_svc(repo)

    result = svc.update("C-1", full_name="  New Name  ")

    assert result.full_name == "New Name"
