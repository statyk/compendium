"""Cross-service tests: fines integrated with circulation + holds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from compendium.config.settings import Settings
from compendium.domain.enums import FineKind, FineStatus, HoldStatus, ItemStatus
from compendium.domain.errors import BlockedByFinesError, BusinessRuleError, ValidationError
from compendium.domain.models import Loan, Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.fines import FineService
from compendium.services.holds import HoldService


def _open_lib_dune(isbn="9780441013593"):
    return {
        "title": "Dune",
        "authors": [{"name": "Frank Herbert"}],
        "publishers": [{"name": "Chilton"}],
        "publish_date": "1965",
        "cover": {},
        "identifiers": {},
    }


def _seed(session, isbn="9780441013593"):
    with patch("compendium.services.metadata.lookup_isbn", return_value=_open_lib_dune()):
        catalog = CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        )
        work, item = catalog.add_from_isbn(isbn)
    session.flush()
    return work, item


def _patron(session, card):
    p = Patron(library_card_number=card, full_name="Alice")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


def _set_policy(session, *, per_day=10, cap=None, grace=0, lost_default=None, proc=None):
    pol = SqlLoanPolicyRepository(session).get_default()
    pol.overdue_fine_per_day_cents = per_day
    pol.overdue_fine_cap_cents = cap
    pol.grace_period_days = grace
    pol.lost_item_default_cents = lost_default
    pol.lost_item_processing_fee_cents = proc
    session.flush()


def _build(session, settings=None):
    settings = settings or Settings(database_url="sqlite:///:memory:")
    audit = AuditService(SqlAuditLogRepository(session))
    fine_svc = FineService(
        fine_repo=SqlFineRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        settings=settings,
        audit_svc=audit,
        source="test",
    )
    circ = CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        fine_svc=fine_svc,
        audit_svc=audit,
        source="test",
    )
    holds = HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
        fine_svc=fine_svc,
    )
    return circ, holds, fine_svc


def test_checkin_auto_assesses_overdue_fine(session):
    _, item = _seed(session)
    patron = _patron(session, "CIRC0001")
    _set_policy(session, per_day=25)
    circ, _, fines = _build(session)

    # Manually put the loan 3 days in the past so it's overdue on checkin
    circ.checkout(item.barcode, "CIRC0001")
    loan = SqlLoanRepository(session).get_active_for_item(item.id)
    loan.due_at = datetime.now(timezone.utc) - timedelta(days=3)
    session.flush()

    circ.checkin(item.barcode)
    f = fines.list(patron_id=patron.id)
    assert len(f) == 1
    assert f[0].kind == FineKind.OVERDUE.value
    assert f[0].amount_cents == 75  # 3 × 25


def test_checkin_no_fine_when_returned_on_time(session):
    _, item = _seed(session)
    patron = _patron(session, "CIRC0002")
    _set_policy(session, per_day=25)
    circ, _, fines = _build(session)

    circ.checkout(item.barcode, "CIRC0002")
    circ.checkin(item.barcode)
    assert fines.list(patron_id=patron.id) == []


def test_checkout_blocked_when_over_threshold(session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_THRESHOLD_CENTS", "100")
    monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_HOLDS", "false")
    from compendium.services import site_settings as _ss
    _ss.invalidate_cache()
    _, item = _seed(session)
    patron = _patron(session, "CIRC0003")
    _set_policy(session, per_day=50)
    circ, _, fines = _build(session)

    # Assess a large manual fine so patron is blocked
    fines.assess_manual(patron, kind=FineKind.OTHER.value, amount_cents=500, note="x")
    with pytest.raises(BlockedByFinesError):
        circ.checkout(item.barcode, "CIRC0003")


def test_checkout_allowed_under_threshold(session):
    _, item = _seed(session)
    patron = _patron(session, "CIRC0004")
    settings = Settings(
        database_url="sqlite:///:memory:", fine_block_threshold_cents=500
    )
    circ, _, fines = _build(session, settings=settings)
    fines.assess_manual(patron, kind=FineKind.OTHER.value, amount_cents=100, note="x")
    loan = circ.checkout(item.barcode, "CIRC0004")
    assert loan is not None


def test_hold_allowed_when_only_checkout_blocked(session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_THRESHOLD_CENTS", "100")
    monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_HOLDS", "false")
    from compendium.services import site_settings as _ss
    _ss.invalidate_cache()
    work, item = _seed(session)
    patron = _patron(session, "CIRC0005")
    circ, holds, fines = _build(session)
    fines.assess_manual(patron, kind=FineKind.OTHER.value, amount_cents=500, note="x")

    # Holds allowed (AVAILABLE copy → immediate promote)
    hold = holds.place(work.id, "CIRC0005")
    assert hold.status == HoldStatus.AVAILABLE.value
    # But checkout blocked
    with pytest.raises(BlockedByFinesError):
        circ.checkout(item.barcode, "CIRC0005")


def test_hold_blocked_when_fine_block_holds_true(session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_THRESHOLD_CENTS", "100")
    monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_HOLDS", "true")
    from compendium.services import site_settings as _ss
    _ss.invalidate_cache()
    work, item = _seed(session)
    patron = _patron(session, "CIRC0006")
    _, holds, fines = _build(session)
    fines.assess_manual(patron, kind=FineKind.OTHER.value, amount_cents=500, note="x")
    with pytest.raises(BlockedByFinesError):
        holds.place(work.id, "CIRC0006")


def test_declare_lost_creates_lost_and_processing_fines_and_cancels_holds(session):
    work, item = _seed(session)
    patron = _patron(session, "CIRC0007")
    _set_policy(session, per_day=0, lost_default=2500, proc=500)
    circ, holds, fines = _build(session)

    circ.checkout(item.barcode, "CIRC0007")

    # Place a hold from another patron on the same work
    other = _patron(session, "CIRC0008")
    hold = holds.place(work.id, "CIRC0008")
    assert hold.status == HoldStatus.WAITING.value

    circ.declare_lost(item.barcode)

    assert item.status == ItemStatus.LOST.value
    # Loan is closed
    assert SqlLoanRepository(session).get_active_for_item(item.id) is None
    # Hold got cancelled
    session.refresh(hold)
    assert hold.status == HoldStatus.CANCELLED.value
    # Two fines for the borrowing patron
    my_fines = fines.list(patron_id=patron.id)
    kinds = {f.kind for f in my_fines}
    assert FineKind.LOST.value in kinds
    assert FineKind.PROCESSING.value in kinds


def test_declare_lost_explicit_cost(session):
    _, item = _seed(session)
    _patron(session, "CIRC0009")
    _set_policy(session, per_day=0, lost_default=2500)
    circ, _, fines = _build(session)
    circ.checkout(item.barcode, "CIRC0009")
    circ.declare_lost(item.barcode, replacement_cost_cents=1000)
    f = [x for x in fines.list() if x.kind == FineKind.LOST.value][0]
    assert f.amount_cents == 1000


def test_declare_lost_missing_cost_and_no_default_fails(session):
    _, item = _seed(session)
    _patron(session, "CIRC0010")
    _set_policy(session)  # no lost_default
    circ, _, _ = _build(session)
    circ.checkout(item.barcode, "CIRC0010")
    with pytest.raises(ValidationError, match="Replacement cost"):
        circ.declare_lost(item.barcode)


def test_declare_lost_rejects_already_lost(session):
    _, item = _seed(session)
    _patron(session, "CIRC0011")
    _set_policy(session, lost_default=1000)
    circ, _, _ = _build(session)
    circ.checkout(item.barcode, "CIRC0011")
    circ.declare_lost(item.barcode)
    with pytest.raises(BusinessRuleError, match="already declared lost"):
        circ.declare_lost(item.barcode)


def test_mark_damaged_requires_note(session):
    _, item = _seed(session)
    _patron(session, "CIRC0012")
    circ, _, _ = _build(session)
    circ.checkout(item.barcode, "CIRC0012")
    with pytest.raises(ValidationError):
        circ.mark_damaged(item.barcode, amount_cents=500, note="")


def test_mark_damaged_creates_fine_and_closes_loan(session):
    _, item = _seed(session)
    patron = _patron(session, "CIRC0013")
    circ, _, fines = _build(session)
    circ.checkout(item.barcode, "CIRC0013")
    circ.mark_damaged(item.barcode, amount_cents=750, note="dropped in puddle")
    assert item.status == ItemStatus.DAMAGED.value
    assert SqlLoanRepository(session).get_active_for_item(item.id) is None
    my_fines = fines.list(patron_id=patron.id)
    assert len(my_fines) == 1
    assert my_fines[0].kind == FineKind.DAMAGED.value
    assert my_fines[0].amount_cents == 750


def test_clear_damage_resets_status_fee_unaffected(session):
    _, item = _seed(session)
    patron = _patron(session, "CIRC0014")
    circ, _, fines = _build(session)
    circ.checkout(item.barcode, "CIRC0014")
    circ.mark_damaged(item.barcode, amount_cents=500, note="scratch")
    before_total = fines.outstanding_total(patron.id)
    circ.clear_damage(item.barcode)
    assert item.status == ItemStatus.AVAILABLE.value
    assert fines.outstanding_total(patron.id) == before_total


def test_clear_damage_rejects_when_not_damaged(session):
    _, item = _seed(session)
    _patron(session, "CIRC0015")
    circ, _, _ = _build(session)
    with pytest.raises(BusinessRuleError, match="not in damaged status"):
        circ.clear_damage(item.barcode)


def test_clear_lost_resets_status(session):
    _, item = _seed(session)
    _patron(session, "CIRC0016")
    _set_policy(session, lost_default=1000)
    circ, _, _ = _build(session)
    circ.checkout(item.barcode, "CIRC0016")
    circ.declare_lost(item.barcode)
    circ.clear_lost(item.barcode)
    assert item.status == ItemStatus.AVAILABLE.value


def test_checkout_not_blocked_when_no_fine_svc_wired(session):
    """CirculationService without fine_svc ignores fines entirely."""
    _, item = _seed(session)
    _patron(session, "CIRC0017")
    # Build WITHOUT fine_svc
    circ = CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )
    loan = circ.checkout(item.barcode, "CIRC0017")
    assert loan is not None
