"""Integration tests: CirculationService respects the library calendar.

Verifies that checkout and renew roll due_at forward past closed days and
that the default (all-days-open) hours preserve existing behaviour.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

import pytest

from compendium.config.settings import Settings
from compendium.domain.models import LoanPolicy, MediaType
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


def _circ(session, cal_svc: CalendarService | None = None) -> CirculationService:
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
        calendar_svc=cal_svc,
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


class TestCheckoutWithDefaultHours:
    def test_checkout_with_default_calendar_lands_at_close_time(self, session):
        """Default hours (all open 00:00–23:59) → due date at 23:59 UTC."""
        _, item = _seed_book(session)
        patron = _patron_svc(session).create(full_name="Alice")
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)  # Monday

        with patch("compendium.services.circulation.datetime") as mock_dt:
            mock_dt.now.return_value = now
            loan = _circ(session, _cal_svc(session)).checkout(item.barcode, patron.library_card_number)

        # Default policy = 14 days. All-open hours → lands at 23:59 on June 15.
        expected = datetime(2026, 6, 15, 23, 59, tzinfo=timezone.utc)
        assert loan.due_at == expected

    def test_checkout_without_calendar_uses_timedelta_only(self, session):
        """No CalendarService → falls back to now + timedelta (original behaviour)."""
        _, item = _seed_book(session)
        patron = _patron_svc(session).create(full_name="Bob")
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

        with patch("compendium.services.circulation.datetime") as mock_dt:
            mock_dt.now.return_value = now
            loan = _circ(session, cal_svc=None).checkout(item.barcode, patron.library_card_number)

        expected = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        assert loan.due_at == expected


class TestCheckoutRollingPastClosedDay:
    def test_due_date_rolls_past_closed_sunday(self, session):
        cal = _cal_svc(session)
        # Close Sunday (weekday 6) and set Mon-Sat close time to 17:00
        for wd in range(6):
            cal.update_weekday(wd, close_time=time(17, 0))
        cal.update_weekday(6, is_open=False)

        _, item = _seed_book(session)
        patron = _patron_svc(session).create(full_name="Carol")
        # Checkout Mon 2026-06-01; 14 days → 2026-06-15 (Monday, open) → 17:00 UTC
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        with patch("compendium.services.circulation.datetime") as mock_dt:
            mock_dt.now.return_value = now
            loan = _circ(session, cal).checkout(item.barcode, patron.library_card_number)

        assert loan.due_at == datetime(2026, 6, 15, 17, 0, tzinfo=timezone.utc)

    def test_due_date_rolls_past_holiday(self, session):
        cal = _cal_svc(session)
        for wd in range(7):
            cal.update_weekday(wd, close_time=time(17, 0))
        cal.add_closed_date(date(2026, 6, 15), label="Holiday")

        _, item = _seed_book(session)
        patron = _patron_svc(session).create(full_name="Dave")
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        with patch("compendium.services.circulation.datetime") as mock_dt:
            mock_dt.now.return_value = now
            loan = _circ(session, cal).checkout(item.barcode, patron.library_card_number)

        # June 15 is closed → rolls to June 16
        assert loan.due_at == datetime(2026, 6, 16, 17, 0, tzinfo=timezone.utc)

    def test_due_date_rolls_past_multi_day_closure(self, session):
        cal = _cal_svc(session)
        for wd in range(7):
            cal.update_weekday(wd, close_time=time(17, 0))
        # Multi-day closure June 13-17
        cal.add_closed_date(date(2026, 6, 13), date(2026, 6, 17), label="Conference")

        _, item = _seed_book(session)
        patron = _patron_svc(session).create(full_name="Eve")
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        with patch("compendium.services.circulation.datetime") as mock_dt:
            mock_dt.now.return_value = now
            loan = _circ(session, cal).checkout(item.barcode, patron.library_card_number)

        # 14 days → June 15 (inside closure June 13-17) → rolls to June 18
        assert loan.due_at == datetime(2026, 6, 18, 17, 0, tzinfo=timezone.utc)


class TestRenewalRolling:
    def test_renew_rolls_past_closed_sunday(self, session):
        cal = _cal_svc(session)
        for wd in range(6):
            cal.update_weekday(wd, close_time=time(17, 0))
        cal.update_weekday(6, is_open=False)  # Sunday closed

        _, item = _seed_book(session)
        patron = _patron_svc(session).create(full_name="Frank")
        # Checkout Mon 2026-06-01; due June 15 (Mon)
        checkout_time = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        with patch("compendium.services.circulation.datetime") as mock_dt:
            mock_dt.now.return_value = checkout_time
            _circ(session, cal).checkout(item.barcode, patron.library_card_number)

        # Renew on Tue 2026-06-02; 14 days → June 16 (Tue, open) → 17:00
        renew_time = datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
        with patch("compendium.services.circulation.datetime") as mock_dt:
            mock_dt.now.return_value = renew_time
            loan = _circ(session, cal).renew(item.barcode, patron.library_card_number)

        assert loan.due_at == datetime(2026, 6, 16, 17, 0, tzinfo=timezone.utc)
