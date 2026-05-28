"""Integration tests for CalendarService: CRUD + DB-backed date queries."""
from __future__ import annotations

from datetime import date, time

import pytest

from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import ClosedDate, LibraryHours
from compendium.repositories.sql.calendar_repository import (
    SqlClosedDateRepository,
    SqlLibraryHoursRepository,
)
from compendium.services.calendar import CalendarService, NoOpenDayError


def _svc(session, tz: str = "UTC") -> CalendarService:
    return CalendarService(
        hours_repo=SqlLibraryHoursRepository(session),
        closed_date_repo=SqlClosedDateRepository(session),
        timezone=tz,
    )


class TestLibraryHoursSeeded:
    def test_seven_rows_seeded_all_open(self, session):
        rows = SqlLibraryHoursRepository(session).list()
        assert len(rows) == 7
        assert all(r.is_open for r in rows)

    def test_default_hours_are_midnight_to_2359(self, session):
        rows = SqlLibraryHoursRepository(session).list()
        for r in rows:
            assert r.open_time == time(0, 0)
            assert r.close_time == time(23, 59)


class TestUpdateWeekday:
    def test_close_sunday(self, session):
        svc = _svc(session)
        row = svc.update_weekday(6, is_open=False)
        assert row.is_open is False
        assert svc.is_open_on(date(2026, 5, 31)) is False  # May 31 2026 = Sunday

    def test_change_close_time(self, session):
        svc = _svc(session)
        svc.update_weekday(0, close_time=time(17, 0))
        row = SqlLibraryHoursRepository(session).get(0)
        assert row.close_time == time(17, 0)

    def test_invalid_weekday_raises(self, session):
        with pytest.raises(ValidationError):
            _svc(session).update_weekday(7)

    def test_weekday_minus_one_raises(self, session):
        with pytest.raises(ValidationError):
            _svc(session).update_weekday(-1)


class TestClosedDateCrud:
    def test_add_single_day(self, session):
        svc = _svc(session)
        cd = svc.add_closed_date(date(2026, 12, 25), label="Christmas")
        assert cd.id is not None
        assert cd.start_date == date(2026, 12, 25)
        assert cd.end_date == date(2026, 12, 25)
        assert cd.label == "Christmas"
        assert cd.recurs_annually is False

    def test_add_range(self, session):
        svc = _svc(session)
        cd = svc.add_closed_date(date(2026, 12, 24), date(2026, 12, 26), label="Winter break")
        assert cd.end_date == date(2026, 12, 26)

    def test_add_annual_recurrence(self, session):
        svc = _svc(session)
        cd = svc.add_closed_date(date(2025, 12, 25), label="Christmas", recurs_annually=True)
        assert cd.recurs_annually is True

    def test_end_before_start_raises(self, session):
        with pytest.raises(ValidationError):
            _svc(session).add_closed_date(date(2026, 12, 26), date(2026, 12, 24))

    def test_delete(self, session):
        svc = _svc(session)
        cd = svc.add_closed_date(date(2026, 7, 4), label="Independence Day")
        svc.delete_closed_date(cd.id)
        assert SqlClosedDateRepository(session).get(cd.id) is None

    def test_delete_nonexistent_raises(self, session):
        with pytest.raises(NotFoundError):
            _svc(session).delete_closed_date(9999)

    def test_update_label(self, session):
        svc = _svc(session)
        cd = svc.add_closed_date(date(2026, 7, 4), label="Old label")
        svc.update_closed_date(cd.id, label="Independence Day")
        assert SqlClosedDateRepository(session).get(cd.id).label == "Independence Day"

    def test_list(self, session):
        svc = _svc(session)
        svc.add_closed_date(date(2026, 7, 4))
        svc.add_closed_date(date(2026, 11, 26))
        result = svc.list_closed_dates()
        assert len(result) == 2


class TestIsOpenOnWithDb:
    def test_open_weekday_with_default_hours(self, session):
        svc = _svc(session)
        # 2026-06-01 is a Monday (weekday 0)
        assert svc.is_open_on(date(2026, 6, 1)) is True

    def test_closed_by_weekday(self, session):
        svc = _svc(session)
        svc.update_weekday(6, is_open=False)  # Sunday
        # 2026-05-31 is a Sunday
        assert svc.is_open_on(date(2026, 5, 31)) is False

    def test_closed_by_holiday(self, session):
        svc = _svc(session)
        svc.add_closed_date(date(2026, 7, 4), label="Independence Day")
        assert svc.is_open_on(date(2026, 7, 4)) is False

    def test_annual_recurrence_in_future_year(self, session):
        svc = _svc(session)
        svc.add_closed_date(date(2025, 12, 25), label="Christmas", recurs_annually=True)
        assert svc.is_open_on(date(2026, 12, 25)) is False
        assert svc.is_open_on(date(2027, 12, 25)) is False
        assert svc.is_open_on(date(2026, 12, 24)) is True
