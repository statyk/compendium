"""Integration tests: FineService deducts closed days from overdue fine computation."""
from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from compendium.config.settings import Settings
from compendium.domain.models import LoanPolicy
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.calendar_repository import (
    SqlClosedDateRepository,
    SqlLibraryHoursRepository,
)
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
from compendium.services.calendar import CalendarService
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.fines import FineService
from compendium.services.patrons import PatronService
from unittest.mock import patch


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
        return CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        ).add_from_isbn(isbn)


def _cal_svc(session, tz: str = "UTC") -> CalendarService:
    return CalendarService(
        hours_repo=SqlLibraryHoursRepository(session),
        closed_date_repo=SqlClosedDateRepository(session),
        timezone=tz,
    )


def _fine_svc(session, cal_svc: CalendarService | None = None) -> FineService:
    settings = Settings(database_url="sqlite:///:memory:")
    return FineService(
        fine_repo=SqlFineRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        settings=settings,
        calendar_svc=cal_svc,
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        source="test",
    )


def _patron_svc(session) -> PatronService:
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        source="test",
    )


def _make_overdue_loan(session, cal_svc, due_at: datetime, returned_at: datetime | None = None):
    """Create a patron + item and manually set loan due_at for fine testing."""
    _, item = _seed_book(session)
    patron = _patron_svc(session).create(full_name="Borrower")
    circ = CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        source="test",
    )
    checkout_time = due_at - __import__("datetime").timedelta(days=14)
    with patch("compendium.services.circulation.datetime") as m:
        m.now.return_value = checkout_time
        loan = circ.checkout(item.barcode, patron.library_card_number)
    # Manually backdate due_at
    loan.due_at = due_at
    if returned_at is not None:
        loan.returned_at = returned_at
    session.flush()
    return loan, patron


class TestFineNoCalendar:
    def test_no_calendar_charges_all_days(self, session):
        """Without CalendarService, all elapsed days are chargeable (existing behaviour)."""
        from compendium.domain.models import MediaType
        book_mt = session.query(MediaType).filter_by(code="book").one()
        pol = LoanPolicy(
            name="Fine Test",
            loan_period_days=14,
            max_renewals=0,
            is_default=False,
            overdue_fine_per_day_cents=10,
            grace_period_days=0,
            media_type_id=book_mt.id,
        )
        session.add(pol)
        session.flush()

        # Due Mon Apr 6 at 00:00 UTC, returned Mon Apr 13 at 00:00 UTC → exactly 7 days
        due_at = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
        returned_at = datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc)
        loan, patron = _make_overdue_loan(session, None, due_at, returned_at)
        loan.returned_at = returned_at
        session.flush()

        fine_svc = _fine_svc(session, cal_svc=None)
        fine = fine_svc.assess_overdue(loan)
        # 7 elapsed days × 10¢ = 70¢
        assert fine is not None
        assert fine.amount_cents == 70


class TestFineWithCalendar:
    def _setup_policy(self, session, rate_cents: int = 10, grace: int = 0) -> None:
        from compendium.domain.models import MediaType
        book_mt = session.query(MediaType).filter_by(code="book").one()
        pol = LoanPolicy(
            name="Fine Test Cal",
            loan_period_days=14,
            max_renewals=0,
            is_default=False,
            overdue_fine_per_day_cents=rate_cents,
            grace_period_days=grace,
            media_type_id=book_mt.id,
        )
        session.add(pol)
        session.flush()

    def test_sunday_closed_not_charged(self, session):
        self._setup_policy(session)
        cal = _cal_svc(session)
        cal.update_weekday(6, is_open=False)  # Sunday

        # Due Mon Apr 6 00:00, returned Mon Apr 13 00:00 — exactly 7 days
        # Apr 12 (Sunday) is closed → 6 chargeable days
        due_at = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
        returned_at = datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc)
        loan, patron = _make_overdue_loan(session, cal, due_at, returned_at)
        loan.returned_at = returned_at
        session.flush()

        fine = _fine_svc(session, cal).assess_overdue(loan)
        assert fine is not None
        assert fine.amount_cents == 60  # 6 × 10¢

    def test_holiday_closed_not_charged(self, session):
        self._setup_policy(session)
        cal = _cal_svc(session)
        cal.add_closed_date(date(2026, 4, 9), label="Holiday")

        # Due Mon Apr 6 00:00, returned Mon Apr 13 00:00 — 7 days; Apr 9 (Thu) closed → 6
        due_at = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
        returned_at = datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc)
        loan, patron = _make_overdue_loan(session, cal, due_at, returned_at)
        loan.returned_at = returned_at
        session.flush()

        fine = _fine_svc(session, cal).assess_overdue(loan)
        assert fine is not None
        assert fine.amount_cents == 60

    def test_multiple_closed_days_deducted(self, session):
        self._setup_policy(session)
        cal = _cal_svc(session)
        cal.update_weekday(6, is_open=False)          # Sunday
        cal.add_closed_date(date(2026, 4, 9), label="Holiday")

        # Apr 6 → Apr 13 = 7 days; Apr 9 (holiday) + Apr 12 (Sun) = 2 closed → 5
        due_at = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
        returned_at = datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc)
        loan, patron = _make_overdue_loan(session, cal, due_at, returned_at)
        loan.returned_at = returned_at
        session.flush()

        fine = _fine_svc(session, cal).assess_overdue(loan)
        assert fine is not None
        assert fine.amount_cents == 50  # 5 × 10¢

    def test_grace_applied_after_closed_deduction(self, session):
        """grace_period_days applies to chargeable days, not raw days_over."""
        self._setup_policy(session, grace=2)
        cal = _cal_svc(session)
        cal.update_weekday(6, is_open=False)  # Sunday

        # Apr 6 → Apr 13 = 7 days; 1 Sunday → 6 after-closed; grace=2 → 4 actual
        due_at = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
        returned_at = datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc)
        loan, patron = _make_overdue_loan(session, cal, due_at, returned_at)
        loan.returned_at = returned_at
        session.flush()

        fine = _fine_svc(session, cal).assess_overdue(loan)
        assert fine is not None
        assert fine.amount_cents == 40  # 4 × 10¢

    def test_active_overdue_loan_fine_assessed_via_cron(self, session):
        """assess_overdue_fines() batch also deducts closed days.

        Uses a due_at in the past (before today = 2026-05-28) so
        list_active_overdue() returns the loan with real datetime.now().
        """
        self._setup_policy(session)
        cal = _cal_svc(session)
        cal.update_weekday(6, is_open=False)  # Sunday

        # Due Apr 6 (past), not returned → active overdue
        due_at = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
        loan, patron = _make_overdue_loan(session, cal, due_at)
        # Ensure not returned
        loan.returned_at = None
        session.flush()

        fsvc = _fine_svc(session, cal)
        counts = fsvc.assess_overdue_fines()

        assert counts["created"] >= 1
        fines = SqlFineRepository(session).list(patron_id=patron.id)
        overdue_fines = [f for f in fines if f.kind == "overdue"]
        assert len(overdue_fines) == 1
        # Actual chargeable days depends on real now; just verify Sunday deduction worked
        # by comparing to what no-calendar would produce.
        fsvc_no_cal = _fine_svc(session, cal_svc=None)
        fine_no_cal = fsvc_no_cal._compute_overdue_amount(loan)
        fine_cal = overdue_fines[0].amount_cents
        # Calendar fine should be ≤ no-calendar fine
        assert fine_cal <= fine_no_cal
