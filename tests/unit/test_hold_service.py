"""Unit tests for HoldService using mock repositories."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from compendium.domain.enums import HoldStatus
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import Branch, Hold, Patron, Work
from compendium.services.holds import HoldService


def _make_hold(status=HoldStatus.WAITING, patron_id=1, work_id=10):
    h = Hold(
        id=1,
        work_id=work_id,
        patron_id=patron_id,
        branch_id=1,
        status=status.value,
        placed_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=30),
    )
    return h


def _service(hold_repo=None, patron_repo=None, work_repo=None, branch_repo=None):
    hold_repo = hold_repo or MagicMock()
    patron_repo = patron_repo or MagicMock()
    work_repo = work_repo or MagicMock()
    branch_repo = branch_repo or MagicMock()
    branch_repo.get_default.return_value = Branch(id=1, code="MAIN", name="Main", is_default=True)
    return HoldService(
        hold_repo=hold_repo,
        patron_repo=patron_repo,
        work_repo=work_repo,
        branch_repo=branch_repo,
        hold_expiry_days=30,
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
