"""FineService: assessment, payment, waiver, materialization, block status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from compendium.config.settings import Settings
from compendium.domain.enums import FineKind, FineStatus, ItemStatus
from compendium.domain.errors import NotFoundError, ValidationError
from compendium.domain.models import Loan, LoanPolicy, Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditAction, AuditService
from compendium.services.catalog import CatalogService
from compendium.services.fines import CheckoutStatus, FineService


def _settings(threshold=None, block_holds=False):
    return Settings(
        database_url="sqlite:///:memory:",
        fine_block_threshold_cents=threshold,
        fine_block_holds=block_holds,
    )


def _fine_svc(session, settings=None, audit=True):
    audit_svc = AuditService(SqlAuditLogRepository(session)) if audit else None
    return FineService(
        fine_repo=SqlFineRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        settings=settings or _settings(),
        audit_svc=audit_svc,
        source="test",
    )


def _seed_work_with_item(session, isbn="9780441013593"):
    from unittest.mock import patch

    with patch(
        "compendium.services.metadata.lookup_isbn",
        return_value={
            "title": "Dune",
            "authors": [{"name": "Frank Herbert"}],
            "publishers": [{"name": "Chilton Books"}],
            "publish_date": "1965",
            "cover": {},
            "identifiers": {},
        },
    ):
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


def _make_patron(session, card="FINE0001"):
    p = Patron(library_card_number=card, full_name="Alice")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


def _set_policy_fines(session, *, per_day=10, cap=None, grace=0, lost_default=None, processing=None):
    """Set fine fields on the default policy."""
    repo = SqlLoanPolicyRepository(session)
    pol = repo.get_default()
    pol.overdue_fine_per_day_cents = per_day
    pol.overdue_fine_cap_cents = cap
    pol.grace_period_days = grace
    pol.lost_item_default_cents = lost_default
    pol.lost_item_processing_fee_cents = processing
    session.flush()
    return pol


def _make_overdue_loan(session, patron, item, days_late: int = 5) -> Loan:
    now = datetime.now(tz=timezone.utc)
    loan = Loan(
        item_id=item.id,
        patron_id=patron.id,
        branch_id=item.branch_id,
        checked_out_at=now - timedelta(days=days_late + 14),
        due_at=now - timedelta(days=days_late),
    )
    SqlLoanRepository(session).add(loan)
    item.status = ItemStatus.CHECKED_OUT.value
    SqlItemRepository(session).update(item)
    return loan


def test_outstanding_total_zero_when_no_fines(session):
    patron = _make_patron(session)
    svc = _fine_svc(session)
    assert svc.outstanding_total(patron.id) == 0


def test_checkout_status_ok_when_no_threshold(session):
    patron = _make_patron(session)
    svc = _fine_svc(session)
    assert svc.checkout_status(patron) == CheckoutStatus.OK


def test_checkout_status_blocked_at_pickup_under_holds_allowed(session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_THRESHOLD_CENTS", "100")
    monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_HOLDS", "false")
    from compendium.services import site_settings as _ss
    _ss.invalidate_cache()
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session, per_day=50)
    loan = _make_overdue_loan(session, patron, item, days_late=10)

    # Book the overdue fine explicitly
    svc = _fine_svc(session)
    svc.assess_overdue(loan)
    # 10 days × 50 = 500 cents > 100 threshold
    assert svc.outstanding_total(patron.id) == 500
    assert svc.checkout_status(patron) == CheckoutStatus.BLOCKED_AT_PICKUP


def test_checkout_status_blocked_when_holds_also_blocked(session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_THRESHOLD_CENTS", "100")
    monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_HOLDS", "true")
    from compendium.services import site_settings as _ss
    _ss.invalidate_cache()
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session, per_day=50)
    loan = _make_overdue_loan(session, patron, item, days_late=10)
    svc = _fine_svc(session)
    svc.assess_overdue(loan)
    assert svc.checkout_status(patron) == CheckoutStatus.BLOCKED


def test_projected_overdue_respects_grace_period(session):
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session, per_day=50, grace=3)
    loan = _make_overdue_loan(session, patron, item, days_late=5)
    svc = _fine_svc(session)
    # 5 days overdue, 3 grace = 2 chargeable × 50 = 100
    assert svc.projected_overdue_fine(loan) == 100


def test_projected_overdue_respects_cap(session):
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session, per_day=100, cap=250)
    loan = _make_overdue_loan(session, patron, item, days_late=10)
    svc = _fine_svc(session)
    assert svc.projected_overdue_fine(loan) == 250


def test_projected_overdue_zero_when_no_rate(session):
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session, per_day=None)
    loan = _make_overdue_loan(session, patron, item, days_late=10)
    svc = _fine_svc(session)
    assert svc.projected_overdue_fine(loan) == 0


def test_assess_overdue_creates_fine_row(session):
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session, per_day=25)
    loan = _make_overdue_loan(session, patron, item, days_late=4)

    svc = _fine_svc(session)
    fine = svc.assess_overdue(loan)
    assert fine is not None
    assert fine.amount_cents == 100
    assert fine.status == FineStatus.OUTSTANDING.value
    assert fine.kind == FineKind.OVERDUE.value


def test_assess_overdue_is_idempotent(session):
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session, per_day=25)
    loan = _make_overdue_loan(session, patron, item, days_late=4)

    svc = _fine_svc(session)
    fine1 = svc.assess_overdue(loan)
    fine2 = svc.assess_overdue(loan)
    assert fine1.id == fine2.id
    # Only one Fine row for this loan
    assert len(svc.list(patron_id=patron.id)) == 1


def test_assess_overdue_updates_amount_on_reassess(session):
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session, per_day=25)
    loan = _make_overdue_loan(session, patron, item, days_late=4)

    svc = _fine_svc(session)
    fine = svc.assess_overdue(loan)
    assert fine.amount_cents == 100

    # Simulate time passing: due date is 4 days ago, push it to 10 days ago
    loan.due_at = datetime.now(tz=timezone.utc) - timedelta(days=10)
    session.flush()
    fine2 = svc.assess_overdue(loan)
    assert fine2.id == fine.id
    assert fine2.amount_cents == 250


def test_assess_overdue_fines_batch_all_patrons(session):
    _, item1 = _seed_work_with_item(session, "9780000000001")
    _, item2 = _seed_work_with_item(session, "9780000000002")
    p1 = _make_patron(session, "CARD0001")
    p2 = _make_patron(session, "CARD0002")
    _set_policy_fines(session, per_day=10)
    _make_overdue_loan(session, p1, item1, days_late=3)
    _make_overdue_loan(session, p2, item2, days_late=5)

    svc = _fine_svc(session)
    counts = svc.assess_overdue_fines()
    assert counts["created"] == 2
    assert counts["updated"] == 0
    assert svc.outstanding_total(p1.id) == 30
    assert svc.outstanding_total(p2.id) == 50


def test_assess_overdue_fines_scoped_to_one_patron(session):
    _, item1 = _seed_work_with_item(session, "9780000000003")
    _, item2 = _seed_work_with_item(session, "9780000000004")
    p1 = _make_patron(session, "CARD0003")
    p2 = _make_patron(session, "CARD0004")
    _set_policy_fines(session, per_day=10)
    _make_overdue_loan(session, p1, item1, days_late=3)
    _make_overdue_loan(session, p2, item2, days_late=5)

    svc = _fine_svc(session)
    counts = svc.assess_overdue_fines(patron_id=p1.id)
    assert counts["created"] == 1
    assert svc.outstanding_total(p1.id) == 30
    assert svc.outstanding_total(p2.id) == 0


def test_assess_overdue_fines_second_run_is_unchanged(session):
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session, per_day=10)
    _make_overdue_loan(session, patron, item, days_late=3)

    svc = _fine_svc(session)
    counts1 = svc.assess_overdue_fines()
    counts2 = svc.assess_overdue_fines()
    assert counts1["created"] == 1
    assert counts2["created"] == 0
    assert counts2["unchanged"] == 1


def test_assess_lost_uses_policy_default(session):
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session, per_day=0, lost_default=2500, processing=500)
    _make_overdue_loan(session, patron, item, days_late=0)

    svc = _fine_svc(session)
    fines = svc.assess_lost(item)
    assert len(fines) == 2
    amounts = {f.kind: f.amount_cents for f in fines}
    assert amounts[FineKind.LOST.value] == 2500
    assert amounts[FineKind.PROCESSING.value] == 500


def test_assess_lost_explicit_cost_overrides_default(session):
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session, per_day=0, lost_default=2500)
    _make_overdue_loan(session, patron, item, days_late=0)

    svc = _fine_svc(session)
    fines = svc.assess_lost(item, replacement_cost_cents=1000)
    assert fines[0].amount_cents == 1000


def test_assess_lost_requires_cost_when_no_default(session):
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _set_policy_fines(session)  # no lost_default
    _make_overdue_loan(session, patron, item, days_late=0)
    svc = _fine_svc(session)
    with pytest.raises(ValidationError, match="Replacement cost"):
        svc.assess_lost(item)


def test_assess_damaged_requires_positive_amount_and_note(session):
    _, item = _seed_work_with_item(session)
    patron = _make_patron(session)
    _make_overdue_loan(session, patron, item)
    svc = _fine_svc(session)
    with pytest.raises(ValidationError):
        svc.assess_damaged(item, amount_cents=0, note="oops")
    with pytest.raises(ValidationError):
        svc.assess_damaged(item, amount_cents=500, note="")


def test_assess_manual_creates_other_fine_with_note(session):
    patron = _make_patron(session)
    svc = _fine_svc(session)
    fine = svc.assess_manual(
        patron,
        kind=FineKind.OTHER.value,
        amount_cents=200,
        note="Replacement card fee",
    )
    assert fine.kind == FineKind.OTHER.value
    assert fine.note == "Replacement card fee"


def test_assess_manual_other_requires_note(session):
    patron = _make_patron(session)
    svc = _fine_svc(session)
    with pytest.raises(ValidationError, match="note"):
        svc.assess_manual(
            patron, kind=FineKind.OTHER.value, amount_cents=200, note=""
        )


def test_pay_transitions_to_paid(session):
    patron = _make_patron(session)
    svc = _fine_svc(session)
    fine = svc.assess_manual(
        patron, kind=FineKind.OTHER.value, amount_cents=500, note="xx"
    )
    paid = svc.pay(fine.id)
    assert paid.status == FineStatus.PAID.value
    assert paid.resolved_at is not None


def test_pay_rejects_already_resolved(session):
    patron = _make_patron(session)
    svc = _fine_svc(session)
    fine = svc.assess_manual(
        patron, kind=FineKind.OTHER.value, amount_cents=500, note="xx"
    )
    svc.pay(fine.id)
    with pytest.raises(ValidationError):
        svc.pay(fine.id)


def test_waive_requires_note(session):
    patron = _make_patron(session)
    svc = _fine_svc(session)
    fine = svc.assess_manual(
        patron, kind=FineKind.OTHER.value, amount_cents=500, note="xx"
    )
    with pytest.raises(ValidationError):
        svc.waive(fine.id, note="")


def test_waive_transitions_to_waived_and_appends_note(session):
    patron = _make_patron(session)
    svc = _fine_svc(session)
    fine = svc.assess_manual(
        patron, kind=FineKind.OTHER.value, amount_cents=500, note="original"
    )
    waived = svc.waive(fine.id, note="compassionate")
    assert waived.status == FineStatus.WAIVED.value
    assert "original" in waived.note
    assert "compassionate" in waived.note


def test_records_audit_entries(session):
    patron = _make_patron(session)
    svc = _fine_svc(session)
    fine = svc.assess_manual(
        patron, kind=FineKind.OTHER.value, amount_cents=500, note="xx"
    )
    svc.pay(fine.id)
    audit = AuditService(SqlAuditLogRepository(session))
    actions = {e.action for e in audit.list(entity_type="fine", entity_id=fine.id)}
    assert AuditAction.ASSESS_FINE in actions
    assert AuditAction.PAY_FINE in actions


def test_outstanding_total_only_counts_outstanding(session):
    patron = _make_patron(session)
    svc = _fine_svc(session)
    f1 = svc.assess_manual(patron, kind=FineKind.OTHER.value, amount_cents=100, note="a")
    f2 = svc.assess_manual(patron, kind=FineKind.OTHER.value, amount_cents=200, note="b")
    svc.pay(f1.id)
    f3 = svc.assess_manual(patron, kind=FineKind.OTHER.value, amount_cents=300, note="c")
    svc.waive(f3.id, note="goodwill")
    # Only f2 is outstanding
    assert svc.outstanding_total(patron.id) == 200
