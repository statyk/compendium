# tests/unit/test_item_note_service.py
"""Unit: ItemNoteService business logic with mock repos."""
import pytest
from unittest.mock import MagicMock

from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import Item, ItemNote
from compendium.services.item_notes import ItemNoteService


def _svc(**overrides):
    defaults = dict(
        item_note_repo=MagicMock(),
        item_repo=MagicMock(),
    )
    defaults.update(overrides)
    return ItemNoteService(**defaults)


def _item(barcode="BC001", id=1):
    return Item(id=id, barcode=barcode, work_id=10, accession_number="A001")


class TestAddNote:
    def test_adds_note(self):
        svc = _svc()
        item = _item()
        svc._item_repo.get_by_barcode.return_value = item
        saved = ItemNote(id=1, item_id=1, kind="general", note="Good condition", is_system=False)
        svc._item_note_repo.add.return_value = saved
        result = svc.add_note("BC001", kind="general", note="Good condition")
        svc._item_note_repo.add.assert_called_once()
        assert isinstance(result, ItemNote)
        assert result.is_system is False

    def test_blank_note_raises_validation_error(self):
        svc = _svc()
        svc._item_repo.get_by_barcode.return_value = _item()
        with pytest.raises(ValidationError):
            svc.add_note("BC001", kind="general", note="")
        with pytest.raises(ValidationError):
            svc.add_note("BC001", kind="general", note="   ")

    def test_status_kind_raises_validation_error(self):
        svc = _svc()
        svc._item_repo.get_by_barcode.return_value = _item()
        with pytest.raises(ValidationError):
            svc.add_note("BC001", kind="status", note="Something")

    def test_invalid_kind_raises_validation_error(self):
        svc = _svc()
        svc._item_repo.get_by_barcode.return_value = _item()
        with pytest.raises(ValidationError):
            svc.add_note("BC001", kind="bogus", note="Something")

    def test_item_not_found_raises(self):
        svc = _svc()
        svc._item_repo.get_by_barcode.return_value = None
        with pytest.raises(NotFoundError):
            svc.add_note("NOTEXIST", kind="general", note="Hello")

    def test_user_id_set_when_actor_present(self):
        actor = MagicMock()
        actor.id = 5
        svc = _svc(actor=actor)
        svc._item_repo.get_by_barcode.return_value = _item()
        # Capture the note passed to add
        captured = []
        def capture_add(note):
            captured.append(note)
            return note
        svc._item_note_repo.add.side_effect = capture_add
        svc.add_note("BC001", kind="general", note="Hello")
        assert len(captured) == 1
        assert captured[0].user_id == 5


class TestListForItem:
    def test_returns_notes(self):
        svc = _svc()
        item = _item()
        svc._item_repo.get_by_barcode.return_value = item
        notes = [
            ItemNote(id=1, item_id=1, kind="general", note="Note A", is_system=False),
            ItemNote(id=2, item_id=1, kind="condition", note="Note B", is_system=True),
        ]
        svc._item_note_repo.list_for_item.return_value = notes
        result = svc.list_for_item("BC001")
        svc._item_note_repo.list_for_item.assert_called_once_with(item.id)
        assert result == notes

    def test_item_not_found_raises(self):
        svc = _svc()
        svc._item_repo.get_by_barcode.return_value = None
        with pytest.raises(NotFoundError):
            svc.list_for_item("NOTEXIST")


class TestDeleteNote:
    def test_deletes_manual_note(self):
        svc = _svc()
        item = _item()
        note = ItemNote(id=10, item_id=1, kind="general", note="A note", is_system=False)
        svc._item_repo.get_by_barcode.return_value = item
        svc._item_note_repo.get.return_value = note
        svc.delete_note("BC001", 10)
        svc._item_note_repo.delete.assert_called_once_with(note)

    def test_system_note_raises_business_rule_error(self):
        svc = _svc()
        item = _item()
        note = ItemNote(id=10, item_id=1, kind="status", note="Checked out", is_system=True)
        svc._item_repo.get_by_barcode.return_value = item
        svc._item_note_repo.get.return_value = note
        with pytest.raises(BusinessRuleError, match="System-generated"):
            svc.delete_note("BC001", 10)

    def test_wrong_item_raises_not_found(self):
        svc = _svc()
        item = _item(barcode="BC001", id=1)
        note = ItemNote(id=10, item_id=99, kind="general", note="A note", is_system=False)
        svc._item_repo.get_by_barcode.return_value = item
        svc._item_note_repo.get.return_value = note
        with pytest.raises(NotFoundError):
            svc.delete_note("BC001", 10)

    def test_note_not_found_raises(self):
        svc = _svc()
        svc._item_repo.get_by_barcode.return_value = _item()
        svc._item_note_repo.get.return_value = None
        with pytest.raises(NotFoundError):
            svc.delete_note("BC001", 999)
