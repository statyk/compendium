"""Integration tests for patron categories: CRUD + checkout integration + expiry."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from compendium.config.settings import Settings
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import LoanPolicy, MediaType, Patron, PatronCategory
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.fines import FineService
from compendium.services.patron_categories import PatronCategoryService
from compendium.services.patrons import PatronService


def _open_lib_dune(isbn="9780441013593"):
    return {
        "title": "Dune",
        "authors": [{"name": "Frank Herbert"}],
        "publishers": [{"name": "Chilton"}],
        "publish_date": "1965",
        "cover": {},
        "identifiers": {},
    }


def _seed_book(session, isbn="9780441013593"):
    with patch("compendium.services.metadata.lookup_isbn", return_value=_open_lib_dune(isbn)):
        catalog = CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        )
        return catalog.add_from_isbn(isbn)


def _patron_svc(session) -> PatronService:
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        source="test",
    )


def _category_svc(session) -> PatronCategoryService:
    return PatronCategoryService(
        repo=SqlPatronCategoryRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        source="test",
    )


def _circ(session) -> CirculationService:
    settings = Settings(database_url="sqlite:///:memory:")
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
    return CirculationService(
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


class TestCategoryCrud:
    def test_seeded_categories_present(self, session):
        repo = SqlPatronCategoryRepository(session)
        codes = {c.code for c in repo.list()}
        assert {"adult", "child", "staff", "teacher"} <= codes
        assert repo.get_default().code == "adult"

    def test_create_normalizes_code(self, session):
        cat = _category_svc(session).create("SENIOR", "Senior")
        assert cat.code == "senior"

    def test_create_rejects_duplicate(self, session):
        with pytest.raises(BusinessRuleError):
            _category_svc(session).create("adult", "Adult")

    def test_default_swap_clears_old_default(self, session):
        svc = _category_svc(session)
        svc.create("staff_lead", "Staff Lead", is_default=True)
        repo = SqlPatronCategoryRepository(session)
        defaults = [c.code for c in repo.list() if c.is_default]
        assert defaults == ["staff_lead"]

    def test_cannot_remove_only_default(self, session):
        adult = SqlPatronCategoryRepository(session).get_by_code("adult")
        with pytest.raises(BusinessRuleError):
            _category_svc(session).update(adult.id, is_default=False)

    def test_cannot_delete_default(self, session):
        adult = SqlPatronCategoryRepository(session).get_by_code("adult")
        with pytest.raises(BusinessRuleError, match="default"):
            _category_svc(session).delete(adult.id)

    def test_cannot_delete_referenced_by_patron(self, session):
        cat = _category_svc(session).create("guest", "Guest")
        p = Patron(
            library_card_number="PCAT0001",
            full_name="Alice",
            category_id=cat.id,
        )
        session.add(p)
        session.flush()
        with pytest.raises(BusinessRuleError, match="patron"):
            _category_svc(session).delete(cat.id)

    def test_cannot_delete_referenced_by_policy(self, session):
        cat = _category_svc(session).create("guest", "Guest")
        pol = LoanPolicy(
            name="Guest policy",
            loan_period_days=7,
            max_renewals=0,
            patron_category_id=cat.id,
        )
        session.add(pol)
        session.flush()
        with pytest.raises(BusinessRuleError, match="polic"):
            _category_svc(session).delete(cat.id)


class TestPatronWithCategoryAndExpiry:
    def test_create_with_category(self, session):
        cat = SqlPatronCategoryRepository(session).get_by_code("child")
        p = _patron_svc(session).create(
            full_name="Kid", category_id=cat.id
        )
        assert p.category_id == cat.id

    def test_create_with_expiry(self, session):
        future = date.today() + timedelta(days=180)
        p = _patron_svc(session).create(full_name="A", expires_at=future)
        assert p.expires_at == future

    def test_update_clears_expiry(self, session):
        future = date.today() + timedelta(days=180)
        p = _patron_svc(session).create(full_name="A", expires_at=future)
        updated = _patron_svc(session).update(
            p.library_card_number, expires_at=None
        )
        assert updated.expires_at is None

    def test_update_contact_fields(self, session):
        p = _patron_svc(session).create(
            full_name="Original Name",
            contact_email="old@example.org",
            contact_phone="555-0100",
        )
        updated = _patron_svc(session).update(
            p.library_card_number,
            full_name="Renamed Person",
            contact_email="new@example.org",
            contact_phone=None,
        )
        assert updated.full_name == "Renamed Person"
        assert updated.contact_email == "new@example.org"
        assert updated.contact_phone is None

    def test_update_full_name_blank_rejected(self, session):
        p = _patron_svc(session).create(full_name="Original Name")
        with pytest.raises(ValidationError):
            _patron_svc(session).update(p.library_card_number, full_name="   ")


class TestExpiryBlocksCheckout:
    def test_expired_patron_cannot_check_out(self, session):
        _, item = _seed_book(session)
        past = date.today() - timedelta(days=1)
        p = _patron_svc(session).create(
            full_name="Expired", expires_at=past
        )
        with pytest.raises(BusinessRuleError, match="expired"):
            _circ(session).checkout(item.barcode, p.library_card_number)

    def test_unexpired_patron_can_check_out(self, session):
        _, item = _seed_book(session)
        future = date.today() + timedelta(days=30)
        p = _patron_svc(session).create(
            full_name="OK", expires_at=future
        )
        loan = _circ(session).checkout(item.barcode, p.library_card_number)
        assert loan is not None

    def test_no_expiry_can_check_out(self, session):
        _, item = _seed_book(session)
        p = _patron_svc(session).create(full_name="Forever")
        loan = _circ(session).checkout(item.barcode, p.library_card_number)
        assert loan is not None


class TestDeactivateExpired:
    def test_flips_only_expired_active_patrons(self, session):
        # Three patrons: expired+active, expired+inactive, future+active.
        past = date.today() - timedelta(days=2)
        future = date.today() + timedelta(days=2)
        a = _patron_svc(session).create(full_name="A", expires_at=past)
        b = _patron_svc(session).create(full_name="B", expires_at=past)
        # b: deactivate manually so it's not double-counted
        b.is_active = False
        session.flush()
        c = _patron_svc(session).create(full_name="C", expires_at=future)
        d = _patron_svc(session).create(full_name="D")  # no expiry

        matches = _patron_svc(session).deactivate_expired()
        ids = {m.id for m in matches}
        assert a.id in ids
        assert b.id not in ids  # already inactive
        assert c.id not in ids  # not yet expired
        assert d.id not in ids  # no expiry
        assert SqlPatronRepository(session).get(a.id).is_active is False

    def test_dry_run_does_not_change_state(self, session):
        past = date.today() - timedelta(days=2)
        a = _patron_svc(session).create(full_name="A", expires_at=past)
        matches = _patron_svc(session).deactivate_expired(dry_run=True)
        assert a.id in {m.id for m in matches}
        assert SqlPatronRepository(session).get(a.id).is_active is True


class TestCategoryAwareLoanPeriod:
    def test_child_book_uses_child_book_policy_period(self, session):
        # Add a "Child Books, 28d" rule. Child patron checks out a book → 28-day loan.
        book = session.query(MediaType).filter_by(code="book").one()
        child = SqlPatronCategoryRepository(session).get_by_code("child")
        pol = LoanPolicy(
            name="Child Books",
            loan_period_days=28,
            max_renewals=2,
            media_type_id=book.id,
            patron_category_id=child.id,
        )
        session.add(pol)
        session.flush()

        _, item = _seed_book(session)
        p = _patron_svc(session).create(
            full_name="Junior", category_id=child.id
        )
        loan = _circ(session).checkout(item.barcode, p.library_card_number)
        diff_days = (loan.due_at - loan.checked_out_at).days
        assert diff_days == 28

    def test_falls_through_to_default_when_no_match(self, session):
        # No specific rules → seeded default (14d).
        _, item = _seed_book(session)
        p = _patron_svc(session).create(full_name="A")
        loan = _circ(session).checkout(item.barcode, p.library_card_number)
        diff_days = (loan.due_at - loan.checked_out_at).days
        assert diff_days == 14
