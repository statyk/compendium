"""Unit tests for HoldService using mock repositories."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import Branch, Hold, Item, Patron, Work
from compendium.services.holds import HoldService


def _make_hold(status=HoldStatus.WAITING, patron_id=1, work_id=10, held_item_id=None):
    h = Hold(
        id=1,
        work_id=work_id,
        patron_id=patron_id,
        branch_id=1,
        status=status.value,
        placed_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=30),
        held_item_id=held_item_id,
    )
    return h


def _service(
    hold_repo=None,
    patron_repo=None,
    work_repo=None,
    branch_repo=None,
    item_repo=None,
    available_copy=None,
):
    hold_repo = hold_repo or MagicMock()
    patron_repo = patron_repo or MagicMock()
    work_repo = work_repo or MagicMock()
    branch_repo = branch_repo or MagicMock()
    item_repo = item_repo or MagicMock()
    branch_repo.get_default.return_value = Branch(id=1, code="MAIN", name="Main", is_default=True)
    # Default: no immediately-available copy, so `place` takes the WAITING path.
    # Tests that exercise immediate promote pass `available_copy=<Item>`.
    work_repo.first_available_loanable_copy.return_value = available_copy
    return HoldService(
        hold_repo=hold_repo,
        patron_repo=patron_repo,
        work_repo=work_repo,
        branch_repo=branch_repo,
        item_repo=item_repo,
        hold_expiry_days=30,
        hold_pickup_days=3,
    )


def test_place_hold_creates_waiting_hold():
    work_repo = MagicMock()
    work_repo.get.return_value = Work(id=10, title="Dune", media_type_id=1)

    patron_repo = MagicMock()
    patron_repo.get_by_card_number.return_value = Patron(
        id=1, library_card_number="C001", full_name="Alice", is_active=True
    )

    hold_repo = MagicMock()
    hold_repo.get_active_for_patron_work.return_value = None
    hold_repo.add.side_effect = lambda h: h

    svc = _service(hold_repo=hold_repo, patron_repo=patron_repo, work_repo=work_repo)
    hold = svc.place(10, "C001")

    assert hold.status == HoldStatus.WAITING.value
    assert hold.work_id == 10
    assert hold.patron_id == 1
    hold_repo.add.assert_called_once()


def test_place_hold_unknown_work_raises():
    work_repo = MagicMock()
    work_repo.get.return_value = None
    svc = _service(work_repo=work_repo)
    with pytest.raises(NotFoundError):
        svc.place(99, "C001")


def test_place_hold_unknown_patron_raises():
    work_repo = MagicMock()
    work_repo.get.return_value = Work(id=10, title="Dune", media_type_id=1)
    patron_repo = MagicMock()
    patron_repo.get_by_card_number.return_value = None
    svc = _service(work_repo=work_repo, patron_repo=patron_repo)
    with pytest.raises(NotFoundError):
        svc.place(10, "BADCARD")


def test_place_hold_inactive_patron_raises():
    work_repo = MagicMock()
    work_repo.get.return_value = Work(id=10, title="Dune", media_type_id=1)
    patron_repo = MagicMock()
    patron_repo.get_by_card_number.return_value = Patron(
        id=1, library_card_number="C001", full_name="Alice", is_active=False
    )
    svc = _service(work_repo=work_repo, patron_repo=patron_repo)
    with pytest.raises(BusinessRuleError, match="not active"):
        svc.place(10, "C001")


def test_place_hold_duplicate_raises():
    work_repo = MagicMock()
    work_repo.get.return_value = Work(id=10, title="Dune", media_type_id=1)
    patron_repo = MagicMock()
    patron_repo.get_by_card_number.return_value = Patron(
        id=1, library_card_number="C001", full_name="Alice", is_active=True
    )
    hold_repo = MagicMock()
    hold_repo.get_active_for_patron_work.return_value = _make_hold()
    svc = _service(hold_repo=hold_repo, patron_repo=patron_repo, work_repo=work_repo)
    with pytest.raises(BusinessRuleError, match="already has an active hold"):
        svc.place(10, "C001")


def test_cancel_hold_sets_cancelled():
    hold = _make_hold()
    hold_repo = MagicMock()
    hold_repo.get.return_value = hold
    hold_repo.update.side_effect = lambda h: h
    svc = _service(hold_repo=hold_repo)
    result = svc.cancel(1, patron_id=1)
    assert result.status == HoldStatus.CANCELLED.value


def test_cancel_hold_wrong_patron_raises():
    hold = _make_hold(patron_id=1)
    hold_repo = MagicMock()
    hold_repo.get.return_value = hold
    svc = _service(hold_repo=hold_repo)
    with pytest.raises(BusinessRuleError, match="does not belong"):
        svc.cancel(1, patron_id=99)


def test_cancel_already_cancelled_raises():
    hold = _make_hold(status=HoldStatus.CANCELLED)
    hold_repo = MagicMock()
    hold_repo.get.return_value = hold
    svc = _service(hold_repo=hold_repo)
    with pytest.raises(BusinessRuleError):
        svc.cancel(1, patron_id=1)


def test_expire_holds_marks_expired():
    h1 = _make_hold()
    h2 = _make_hold()
    h2.id = 2
    hold_repo = MagicMock()
    hold_repo.get_expired_waiting.return_value = [h1, h2]
    hold_repo.update.side_effect = lambda h: h
    svc = _service(hold_repo=hold_repo)
    count = svc.expire_holds()
    assert count == 2
    assert h1.status == HoldStatus.EXPIRED.value
    assert h2.status == HoldStatus.EXPIRED.value


def test_expire_holds_nothing_to_expire():
    hold_repo = MagicMock()
    hold_repo.get_expired_waiting.return_value = []
    svc = _service(hold_repo=hold_repo)
    assert svc.expire_holds() == 0


def test_place_hold_with_available_copy_promotes_immediately():
    """If a loanable AVAILABLE copy exists, the new hold is AVAILABLE right
    away and the item flips to ON_HOLD."""
    work_repo = MagicMock()
    work_repo.get.return_value = Work(id=10, title="Dune", media_type_id=1)
    work_repo.has_loanable_item.return_value = True

    patron_repo = MagicMock()
    patron_repo.get_by_card_number.return_value = Patron(
        id=1, library_card_number="C001", full_name="Alice", is_active=True
    )

    copy = Item(id=77, work_id=10, barcode="B-1", status=ItemStatus.AVAILABLE.value, is_loanable=True)

    hold_repo = MagicMock()
    hold_repo.get_active_for_patron_work.return_value = None
    hold_repo.add.side_effect = lambda h: h

    item_repo = MagicMock()
    item_repo.update.side_effect = lambda i: i

    svc = _service(
        hold_repo=hold_repo,
        patron_repo=patron_repo,
        work_repo=work_repo,
        item_repo=item_repo,
        available_copy=copy,
    )
    hold = svc.place(10, "C001")

    assert hold.status == HoldStatus.AVAILABLE.value
    assert hold.held_item_id == 77
    assert hold.notified_at is not None
    assert copy.status == ItemStatus.ON_HOLD.value
    item_repo.update.assert_called_once_with(copy)


def test_cancel_available_hold_releases_item_when_no_queue():
    """Cancelling an AVAILABLE hold with no waiting queue frees its copy."""
    copy = Item(id=77, work_id=10, status=ItemStatus.ON_HOLD.value, is_loanable=True)
    hold = _make_hold(status=HoldStatus.AVAILABLE, held_item_id=77)

    hold_repo = MagicMock()
    hold_repo.get.return_value = hold
    hold_repo.get_oldest_waiting_for_work.return_value = None
    hold_repo.update.side_effect = lambda h: h

    item_repo = MagicMock()
    item_repo.get.return_value = copy
    item_repo.update.side_effect = lambda i: i

    svc = _service(hold_repo=hold_repo, item_repo=item_repo)
    result = svc.cancel(1, patron_id=1)

    assert result.status == HoldStatus.CANCELLED.value
    assert result.held_item_id is None
    assert copy.status == ItemStatus.AVAILABLE.value


def test_cancel_available_hold_promotes_next_waiting():
    """Cancelling an AVAILABLE hold with a WAITING queue promotes the next."""
    copy = Item(id=77, work_id=10, status=ItemStatus.ON_HOLD.value, is_loanable=True)
    hold = _make_hold(status=HoldStatus.AVAILABLE, held_item_id=77)
    next_hold = Hold(
        id=2,
        work_id=10,
        patron_id=2,
        branch_id=1,
        status=HoldStatus.WAITING.value,
        placed_at=datetime.utcnow(),
    )

    hold_repo = MagicMock()
    hold_repo.get.return_value = hold
    hold_repo.get_oldest_waiting_for_work.return_value = next_hold
    hold_repo.update.side_effect = lambda h: h

    item_repo = MagicMock()
    item_repo.get.return_value = copy

    svc = _service(hold_repo=hold_repo, item_repo=item_repo)
    svc.cancel(1, patron_id=1)

    assert next_hold.status == HoldStatus.AVAILABLE.value
    assert next_hold.held_item_id == 77
    assert next_hold.notified_at is not None
    # Copy stays ON_HOLD — just for the next patron now.
    assert copy.status == ItemStatus.ON_HOLD.value
