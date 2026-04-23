"""Tests for CatalogService.set_loanable: invariants + auto-cancel of holds."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.errors import NotFoundError, ValidationError
from compendium.domain.models import Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditAction, AuditService
from compendium.services.catalog import CatalogService
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


def _catalog(session, audit_svc=None) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=audit_svc,
        hold_repo=SqlHoldRepository(session),
    )


def _holds(session) -> HoldService:
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
    )


@pytest.fixture
def work_and_item(session):
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        work, item = _catalog(session).add_from_isbn(_ISBN)
    session.flush()
    return work, item


@pytest.fixture
def patron(session):
    p = Patron(library_card_number="LOAN0001", full_name="Alice")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


@pytest.fixture
def patron2(session):
    p = Patron(library_card_number="LOAN0002", full_name="Bob")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


def test_unknown_barcode_raises(session):
    with pytest.raises(NotFoundError):
        _catalog(session).set_loanable("NOPE", is_loanable=False, reason="reference")


def test_flip_off_requires_reason(session, work_and_item):
    _, item = work_and_item
    with pytest.raises(ValidationError, match="reason is required"):
        _catalog(session).set_loanable(item.barcode, is_loanable=False)


def test_invalid_reason_raises(session, work_and_item):
    _, item = work_and_item
    with pytest.raises(ValidationError, match="Unknown reason"):
        _catalog(session).set_loanable(item.barcode, is_loanable=False, reason="bogus")


def test_other_requires_note(session, work_and_item):
    _, item = work_and_item
    with pytest.raises(ValidationError, match="note is required"):
        _catalog(session).set_loanable(item.barcode, is_loanable=False, reason="other")


def test_flip_off_with_other_and_note(session, work_and_item):
    _, item = work_and_item
    _catalog(session).set_loanable(
        item.barcode, is_loanable=False, reason="other", note="donor restriction"
    )
    assert item.is_loanable is False
    assert item.loan_restriction_reason == "other"
    assert item.loan_restriction_note == "donor restriction"


def test_non_other_reason_wipes_note(session, work_and_item):
    _, item = work_and_item
    _catalog(session).set_loanable(
        item.barcode, is_loanable=False, reason="reference", note="ignored"
    )
    assert item.loan_restriction_reason == "reference"
    assert item.loan_restriction_note is None


def test_flip_on_wipes_reason_and_note(session, work_and_item):
    _, item = work_and_item
    _catalog(session).set_loanable(
        item.barcode, is_loanable=False, reason="other", note="temp"
    )
    _catalog(session).set_loanable(item.barcode, is_loanable=True)
    assert item.is_loanable is True
    assert item.loan_restriction_reason is None
    assert item.loan_restriction_note is None


def test_flip_off_last_copy_cancels_holds(session, work_and_item, patron, patron2):
    work, item = work_and_item
    # First place promotes immediately onto the AVAILABLE copy; second stays WAITING.
    h1 = _holds(session).place(work.id, patron.library_card_number)
    h2 = _holds(session).place(work.id, patron2.library_card_number)
    assert h1.status == HoldStatus.AVAILABLE.value
    assert h2.status == HoldStatus.WAITING.value

    _catalog(session).set_loanable(
        item.barcode, is_loanable=False, reason="reference"
    )
    session.flush()

    assert h1.status == HoldStatus.CANCELLED.value
    assert h2.status == HoldStatus.CANCELLED.value


def test_flip_off_when_another_copy_still_loanable_keeps_holds(
    session, work_and_item, patron
):
    work, item = work_and_item
    extra = _catalog(session).add_item_to_work(work.id)
    session.flush()
    # First place promotes onto the lowest-accession copy (the original `item`).
    h = _holds(session).place(work.id, patron.library_card_number)
    assert h.status == HoldStatus.AVAILABLE.value
    assert h.held_item_id == item.id

    _catalog(session).set_loanable(
        item.barcode, is_loanable=False, reason="reference"
    )
    session.flush()

    # `extra` is still loanable, so the hold is preserved but demoted back
    # to WAITING and unpinned (normal promotion will reassign a copy).
    assert extra.is_loanable is True
    assert h.status == HoldStatus.WAITING.value
    assert h.held_item_id is None


def test_flip_off_on_hold_item_drops_to_available(session, work_and_item, patron):
    _, item = work_and_item
    item.status = ItemStatus.ON_HOLD.value
    session.flush()

    _catalog(session).set_loanable(
        item.barcode, is_loanable=False, reason="archive"
    )
    session.flush()

    assert item.status == ItemStatus.AVAILABLE.value


def test_audit_records_set_loanable(session, work_and_item):
    _, item = work_and_item
    audit = AuditService(SqlAuditLogRepository(session))
    _catalog(session, audit_svc=audit).set_loanable(
        item.barcode, is_loanable=False, reason="reference"
    )
    entries = audit.list(entity_type="item", entity_id=item.id)
    assert any(e.action == AuditAction.SET_LOANABLE for e in entries)
    entry = next(e for e in entries if e.action == AuditAction.SET_LOANABLE)
    assert entry.details["is_loanable"] is False
    assert entry.details["reason"] == "reference"


def test_audit_records_auto_cancelled_hold_ids(session, work_and_item, patron, patron2):
    """When flip-off cancels a WAITING hold, the audit entry lists the hold id."""
    from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
    from compendium.repositories.sql.loan_repository import SqlLoanRepository
    from compendium.services.circulation import CirculationService

    work, item = work_and_item
    audit = AuditService(SqlAuditLogRepository(session))
    # Check out first so the subsequent hold stays WAITING (not promoted).
    circ = CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )
    circ.checkout(item.barcode, patron.library_card_number)
    hold = _holds(session).place(work.id, patron2.library_card_number)
    assert hold.status == HoldStatus.WAITING.value
    _catalog(session, audit_svc=audit).set_loanable(
        item.barcode, is_loanable=False, reason="reference"
    )
    entry = next(
        e
        for e in audit.list(entity_type="item", entity_id=item.id)
        if e.action == AuditAction.SET_LOANABLE
    )
    assert entry.details["auto_cancelled_hold_ids"] == [hold.id]


def test_has_loanable_item_excludes_withdrawn(session, work_and_item):
    work, item = work_and_item
    repo = SqlWorkRepository(session)
    assert repo.has_loanable_item(work.id) is True
    item.status = ItemStatus.WITHDRAWN.value
    session.flush()
    assert repo.has_loanable_item(work.id) is False


def test_has_loanable_item_excludes_lost(session, work_and_item):
    work, item = work_and_item
    repo = SqlWorkRepository(session)
    item.status = ItemStatus.LOST.value
    session.flush()
    assert repo.has_loanable_item(work.id) is False


def test_has_loanable_item_excludes_damaged(session, work_and_item):
    work, item = work_and_item
    repo = SqlWorkRepository(session)
    item.status = ItemStatus.DAMAGED.value
    session.flush()
    assert repo.has_loanable_item(work.id) is False


def test_has_loanable_item_excludes_non_loanable(session, work_and_item):
    work, item = work_and_item
    item.is_loanable = False
    item.loan_restriction_reason = "reference"
    session.flush()
    assert SqlWorkRepository(session).has_loanable_item(work.id) is False


def test_no_change_is_idempotent_no_audit(session, work_and_item):
    _, item = work_and_item
    audit = AuditService(SqlAuditLogRepository(session))
    _catalog(session, audit_svc=audit).set_loanable(item.barcode, is_loanable=True)
    entries = audit.list(entity_type="item", entity_id=item.id)
    assert not any(e.action == AuditAction.SET_LOANABLE for e in entries)


# ── CLI ───────────────────────────────────────────────────────────────────────


def _run_item_cli(session, args):
    @contextmanager
    def _scope():
        yield session

    from compendium.cli.commands.item import app as item_app

    runner = CliRunner()
    with patch("compendium.cli.commands.item.session_scope", _scope):
        return runner.invoke(item_app, args)


def test_cli_set_loanable_no_requires_reason(session, work_and_item):
    _, item = work_and_item
    result = _run_item_cli(session, ["set-loanable", "--barcode", item.barcode, "--no"])
    assert result.exit_code == 1
    assert "reason is required" in (result.stderr or result.output).lower()


def test_cli_set_loanable_off_with_reason(session, work_and_item):
    _, item = work_and_item
    result = _run_item_cli(
        session,
        ["set-loanable", "--barcode", item.barcode, "--no", "--reason", "reference"],
    )
    assert result.exit_code == 0
    assert "Loanable : no" in result.output
    assert item.is_loanable is False
    assert item.loan_restriction_reason == "reference"


def test_cli_set_loanable_yes_clears_reason(session, work_and_item):
    _, item = work_and_item
    _run_item_cli(
        session,
        ["set-loanable", "--barcode", item.barcode, "--no", "--reason", "reference"],
    )
    result = _run_item_cli(
        session, ["set-loanable", "--barcode", item.barcode, "--yes"]
    )
    assert result.exit_code == 0
    assert item.is_loanable is True
    assert item.loan_restriction_reason is None


def test_cli_set_loanable_requires_exactly_one_flag(session, work_and_item):
    _, item = work_and_item
    result = _run_item_cli(
        session, ["set-loanable", "--barcode", item.barcode, "--yes", "--no"]
    )
    assert result.exit_code == 1
    result2 = _run_item_cli(session, ["set-loanable", "--barcode", item.barcode])
    assert result2.exit_code == 1
