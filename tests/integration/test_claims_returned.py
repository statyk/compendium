"""Service-level tests for claims-returned state transitions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from compendium.config.settings import Settings
from compendium.domain.enums import FineKind, FineStatus, HoldStatus, ItemStatus
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import Patron
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
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.fines import FineService


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
    with patch("compendium.services.metadata.lookup_isbn", return_value=_open_lib_dune(isbn)):
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
    return circ, fine_svc


class TestClaimReturnedTransition:
    def test_claim_marks_item_claims_returned(self, session):
        _, item = _seed(session)
        p = _patron(session, "CR0001")
        circ, _ = _build(session)
        circ.checkout(item.barcode, "CR0001")

        result = circ.claim_returned(item.barcode, note="Returned last Tuesday")
        assert result.status == ItemStatus.CLAIMS_RETURNED.value
        # Loan stays open
        loan = SqlLoanRepository(session).get_active_for_item(item.id)
        assert loan is not None
        assert loan.returned_at is None

    def test_claim_writes_audit_entry(self, session):
        _, item = _seed(session)
        _patron(session, "CR0002")
        circ, _ = _build(session)
        circ.checkout(item.barcode, "CR0002")
        circ.claim_returned(item.barcode, note="Note")

        entries = AuditService(SqlAuditLogRepository(session)).list(
            entity_type=AuditEntityType.ITEM, entity_id=item.id, limit=10
        )
        actions = [e.action for e in entries]
        assert AuditAction.CLAIM_RETURNED in actions

    def test_cannot_claim_available_item(self, session):
        _, item = _seed(session)
        circ, _ = _build(session)
        with pytest.raises(BusinessRuleError, match="not currently checked out"):
            circ.claim_returned(item.barcode)

    def test_cannot_claim_twice(self, session):
        _, item = _seed(session)
        _patron(session, "CR0003")
        circ, _ = _build(session)
        circ.checkout(item.barcode, "CR0003")
        circ.claim_returned(item.barcode)
        with pytest.raises(BusinessRuleError, match="already marked claims-returned"):
            circ.claim_returned(item.barcode)

    def test_unknown_barcode_raises_not_found(self, session):
        circ, _ = _build(session)
        with pytest.raises(NotFoundError):
            circ.claim_returned("NOSUCH")


class TestVerifyReturned:
    def test_verify_closes_loan_and_releases_item(self, session):
        _, item = _seed(session)
        _patron(session, "CR0010")
        circ, _ = _build(session)
        circ.checkout(item.barcode, "CR0010")
        circ.claim_returned(item.barcode)

        loan = circ.verify_returned(item.barcode)
        assert loan.returned_at is not None
        # Item status transitions back to AVAILABLE
        fresh = SqlItemRepository(session).get_by_barcode(item.barcode)
        assert fresh.status == ItemStatus.AVAILABLE.value

    def test_verify_audits_claim_verified(self, session):
        _, item = _seed(session)
        _patron(session, "CR0011")
        circ, _ = _build(session)
        circ.checkout(item.barcode, "CR0011")
        circ.claim_returned(item.barcode)
        circ.verify_returned(item.barcode)

        entries = AuditService(SqlAuditLogRepository(session)).list(
            entity_type=AuditEntityType.ITEM, entity_id=item.id, limit=10
        )
        actions = [e.action for e in entries]
        assert AuditAction.CLAIM_VERIFIED in actions

    def test_verify_rejects_non_claims_item(self, session):
        _, item = _seed(session)
        _patron(session, "CR0012")
        circ, _ = _build(session)
        circ.checkout(item.barcode, "CR0012")
        # Not yet claimed — still CHECKED_OUT
        with pytest.raises(BusinessRuleError, match="not in claims-returned"):
            circ.verify_returned(item.barcode)

    def test_verify_assesses_overdue_when_late(self, session):
        _, item = _seed(session)
        p = _patron(session, "CR0013")
        # Policy: 25¢/day, no grace
        pol = SqlLoanPolicyRepository(session).get_default()
        pol.overdue_fine_per_day_cents = 25
        pol.grace_period_days = 0
        session.flush()

        circ, fines = _build(session)
        circ.checkout(item.barcode, "CR0013")
        # Fast-forward the due date 5 days into the past
        loan = SqlLoanRepository(session).get_active_for_item(item.id)
        loan.due_at = datetime.now(timezone.utc) - timedelta(days=5)
        session.flush()

        circ.claim_returned(item.barcode)
        circ.verify_returned(item.barcode)

        f = fines.list(patron_id=p.id)
        assert any(x.kind == FineKind.OVERDUE.value and x.amount_cents > 0 for x in f)


class TestWriteOffClaim:
    def test_write_off_closes_loan_without_fines(self, session):
        _, item = _seed(session)
        p = _patron(session, "CR0020")
        circ, fines = _build(session)
        circ.checkout(item.barcode, "CR0020")
        circ.claim_returned(item.barcode)

        loan = circ.write_off_claim(item.barcode, note="Trust the patron")
        assert loan.returned_at is not None
        # No new overdue fine was assessed by this method.
        f_list = fines.list(patron_id=p.id)
        # No fines of kind OVERDUE should be present (even if the loan was late,
        # write-off doesn't assess).
        assert not any(
            f.kind == FineKind.OVERDUE.value and f.status == FineStatus.OUTSTANDING.value
            for f in f_list
        )

    def test_write_off_audits(self, session):
        _, item = _seed(session)
        _patron(session, "CR0021")
        circ, _ = _build(session)
        circ.checkout(item.barcode, "CR0021")
        circ.claim_returned(item.barcode)
        circ.write_off_claim(item.barcode, note="reason")

        entries = AuditService(SqlAuditLogRepository(session)).list(
            entity_type=AuditEntityType.ITEM, entity_id=item.id, limit=10
        )
        actions = [e.action for e in entries]
        assert AuditAction.CLAIM_WRITE_OFF in actions

    def test_write_off_requires_note(self, session):
        _, item = _seed(session)
        _patron(session, "CR0022")
        circ, _ = _build(session)
        circ.checkout(item.barcode, "CR0022")
        circ.claim_returned(item.barcode)
        with pytest.raises(ValidationError, match="note"):
            circ.write_off_claim(item.barcode, note="")


class TestEscalateToLost:
    def test_declare_lost_from_claims_returned_works(self, session):
        _, item = _seed(session)
        p = _patron(session, "CR0030")
        # Policy: lost default cost
        pol = SqlLoanPolicyRepository(session).get_default()
        pol.lost_item_default_cents = 2000
        session.flush()

        circ, fines = _build(session)
        circ.checkout(item.barcode, "CR0030")
        circ.claim_returned(item.barcode)

        item2 = circ.declare_lost(item.barcode, note="investigation failed")
        assert item2.status == ItemStatus.LOST.value
        # Lost fine is assessed
        f = fines.list(patron_id=p.id)
        assert any(x.kind == FineKind.LOST.value for x in f)


class TestHasLoanableItem:
    def test_claims_returned_counts_as_recoverable(self, session):
        work, item = _seed(session)
        _patron(session, "CR0040")
        circ, _ = _build(session)
        circ.checkout(item.barcode, "CR0040")
        circ.claim_returned(item.barcode)
        # has_loanable_item should still return True — the copy might turn up
        assert SqlWorkRepository(session).has_loanable_item(work.id) is True
