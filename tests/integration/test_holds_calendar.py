"""Integration tests: HoldService rolls hold pickup expiry past closed days."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

import pytest

from compendium.domain.enums import HoldStatus
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.calendar_repository import (
    SqlClosedDateRepository,
    SqlLibraryHoursRepository,
)
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.calendar import CalendarService
from compendium.services.catalog import CatalogService
from compendium.services.holds import HoldService
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


def _holds_svc(session, cal_svc: CalendarService | None = None) -> HoldService:
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        item_repo=SqlItemRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_pickup_days=3,
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


class TestHoldPickupExpiry:
    def test_no_calendar_uses_plain_timedelta(self, session):
        """Without calendar, expires_at = now + pickup_days (original behaviour)."""
        work, item = _seed_book(session)
        patron = _patron_svc(session).create(full_name="Alice")
        now = datetime(2026, 6, 5, 10, 0, tzinfo=timezone.utc)  # Friday

        with patch("compendium.services.holds.datetime") as mock_dt:
            mock_dt.now.return_value = now
            hold = _holds_svc(session, cal_svc=None).place(
                work.id, patron.library_card_number
            )

        assert hold.status == HoldStatus.AVAILABLE.value
        assert hold.expires_at == now + timedelta(days=3)

    def test_pickup_expiry_rolls_past_closed_sunday(self, session):
        """Hold placed on Friday; 3-day pickup window; Sunday closed → Mon."""
        cal = _cal_svc(session)
        for wd in range(6):
            cal.update_weekday(wd, close_time=time(17, 0))
        cal.update_weekday(6, is_open=False)  # Sunday

        work, item = _seed_book(session)
        patron = _patron_svc(session).create(full_name="Bob")
        # Placed Friday Jun 5 → 3 days → Mon Jun 8 (skipping Sun Jun 8... wait)
        # Jun 5 + 3 days = Jun 8. Jun 8 is Monday, open. No rolling needed.
        # For Sunday to be hit: Jun 5 + 3 = Jun 8 Mon (open). Let me use Jun 4 Thu → Jun 7 Sun → roll to Jun 8 Mon.
        now = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)  # Thursday

        with patch("compendium.services.holds.datetime") as mock_dt:
            mock_dt.now.return_value = now
            hold = _holds_svc(session, cal).place(
                work.id, patron.library_card_number
            )

        # Jun 4 + 3 days = Jun 7 (Sunday, closed) → rolls to Jun 8 (Monday) at 17:00 UTC
        assert hold.expires_at == datetime(2026, 6, 8, 17, 0, tzinfo=timezone.utc)

    def test_pickup_expiry_rolls_past_holiday(self, session):
        """Hold placed on day where pickup falls on a holiday."""
        cal = _cal_svc(session)
        for wd in range(7):
            cal.update_weekday(wd, close_time=time(17, 0))
        cal.add_closed_date(date(2026, 6, 7), label="Library Day Off")

        work, item = _seed_book(session)
        patron = _patron_svc(session).create(full_name="Carol")
        now = datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)  # Thursday

        with patch("compendium.services.holds.datetime") as mock_dt:
            mock_dt.now.return_value = now
            hold = _holds_svc(session, cal).place(
                work.id, patron.library_card_number
            )

        # Jun 4 + 3 = Jun 7 (holiday) → rolls to Jun 8 at 17:00 UTC
        assert hold.expires_at == datetime(2026, 6, 8, 17, 0, tzinfo=timezone.utc)
