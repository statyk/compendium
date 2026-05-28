"""Unit tests for CalendarService pure date-math logic.

These tests use mock repositories — no DB required. They cover:
- is_open_on / next_open_date
- compute_due_at (due-date rolling, UTC↔local)
- closed_days_between (fine deduction)
- Annual-recurrence expansion
- DST edge cases (America/New_York)
- All-closed safety guard
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from compendium.domain.models import ClosedDate, LibraryHours
from compendium.services.calendar import CalendarService, NoOpenDayError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hours_all_open() -> list[LibraryHours]:
    """All seven weekdays open 09:00–17:00 in whatever tz the caller uses."""
    return [
        LibraryHours(weekday=wd, is_open=True, open_time=time(9, 0), close_time=time(17, 0))
        for wd in range(7)
    ]


def _hours_sun_closed() -> list[LibraryHours]:
    """Mon–Sat open 09:00–17:00; Sunday (weekday 6) closed."""
    result = []
    for wd in range(7):
        if wd == 6:  # Sunday
            result.append(LibraryHours(weekday=wd, is_open=False))
        else:
            result.append(
                LibraryHours(weekday=wd, is_open=True, open_time=time(9, 0), close_time=time(17, 0))
            )
    return result


def _no_closed_dates() -> list[ClosedDate]:
    return []


def _make_svc(
    hours: list[LibraryHours] | None = None,
    closed_dates: list[ClosedDate] | None = None,
    tz: str = "UTC",
) -> CalendarService:
    hours_repo = MagicMock()
    hours_repo.list.return_value = hours if hours is not None else _hours_all_open()
    closed_repo = MagicMock()
    closed_repo.list_in_range.return_value = closed_dates if closed_dates is not None else []
    return CalendarService(hours_repo=hours_repo, closed_date_repo=closed_repo, timezone=tz)


# ---------------------------------------------------------------------------
# is_open_on
# ---------------------------------------------------------------------------

class TestIsOpenOn:
    def test_open_weekday(self):
        svc = _make_svc(_hours_all_open())
        # 2026-05-25 is a Monday (weekday 0)
        assert svc.is_open_on(date(2026, 5, 25)) is True

    def test_closed_weekday(self):
        svc = _make_svc(_hours_sun_closed())
        # 2026-05-31 is a Sunday (weekday 6)
        assert svc.is_open_on(date(2026, 5, 31)) is False

    def test_closed_date_overrides_open_weekday(self):
        closed = [ClosedDate(start_date=date(2026, 5, 25), end_date=date(2026, 5, 25), label="Memorial Day")]
        svc = _make_svc(_hours_all_open(), closed)
        assert svc.is_open_on(date(2026, 5, 25)) is False

    def test_closed_date_range_covers_multiple_days(self):
        closed = [ClosedDate(start_date=date(2026, 12, 24), end_date=date(2026, 12, 26), label="Winter break")]
        svc = _make_svc(_hours_all_open(), closed)
        assert svc.is_open_on(date(2026, 12, 24)) is False
        assert svc.is_open_on(date(2026, 12, 25)) is False
        assert svc.is_open_on(date(2026, 12, 26)) is False
        assert svc.is_open_on(date(2026, 12, 27)) is True

    def test_annual_recurrence_matches_same_date_next_year(self):
        # 2025-12-25 recurring annual — must match 2026-12-25
        closed = [ClosedDate(start_date=date(2025, 12, 25), end_date=date(2025, 12, 25),
                              label="Christmas", recurs_annually=True)]
        svc = _make_svc(_hours_all_open(), closed)
        assert svc.is_open_on(date(2025, 12, 25)) is False
        assert svc.is_open_on(date(2026, 12, 25)) is False
        assert svc.is_open_on(date(2027, 12, 25)) is False
        assert svc.is_open_on(date(2026, 12, 24)) is True  # day before

    def test_annual_recurrence_range(self):
        # A two-day range recurring: Dec 24–25
        closed = [ClosedDate(start_date=date(2025, 12, 24), end_date=date(2025, 12, 25),
                              label="Christmas", recurs_annually=True)]
        svc = _make_svc(_hours_all_open(), closed)
        assert svc.is_open_on(date(2026, 12, 24)) is False
        assert svc.is_open_on(date(2026, 12, 25)) is False
        assert svc.is_open_on(date(2026, 12, 26)) is True


# ---------------------------------------------------------------------------
# next_open_date
# ---------------------------------------------------------------------------

class TestNextOpenDate:
    def test_open_day_returns_same_day(self):
        svc = _make_svc(_hours_all_open())
        assert svc.next_open_date(date(2026, 5, 25)) == date(2026, 5, 25)

    def test_rolls_over_closed_sunday(self):
        svc = _make_svc(_hours_sun_closed())
        # Sunday → next open is Monday
        assert svc.next_open_date(date(2026, 5, 31)) == date(2026, 6, 1)

    def test_rolls_over_multi_day_break(self):
        closed = [ClosedDate(start_date=date(2026, 12, 24), end_date=date(2026, 12, 26), label="Break")]
        svc = _make_svc(_hours_all_open(), closed)
        # Dec 24 → rolls forward to Dec 27
        assert svc.next_open_date(date(2026, 12, 24)) == date(2026, 12, 27)

    def test_all_closed_raises(self):
        hours = [LibraryHours(weekday=wd, is_open=False) for wd in range(7)]
        svc = _make_svc(hours, [])
        with pytest.raises(NoOpenDayError):
            svc.next_open_date(date(2026, 5, 25))


# ---------------------------------------------------------------------------
# compute_due_at
# ---------------------------------------------------------------------------

class TestComputeDueAt:
    def test_open_day_returns_close_time_utc(self):
        # All-open 09:00–17:00 UTC, checkout at noon UTC
        svc = _make_svc(_hours_all_open(), tz="UTC")
        checkout = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)  # Monday
        due = svc.compute_due_at(checkout, 14)
        # 14 days later = Mon 2026-06-08; close at 17:00 UTC
        assert due == datetime(2026, 6, 8, 17, 0, tzinfo=timezone.utc)

    def test_rolls_past_closed_sunday(self):
        svc = _make_svc(_hours_sun_closed(), tz="UTC")
        # checkout Thu 2026-05-21, 7-day loan → 2026-05-28 (Thursday) — open
        checkout = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
        due = svc.compute_due_at(checkout, 7)
        assert due == datetime(2026, 5, 28, 17, 0, tzinfo=timezone.utc)

    def test_rolls_past_closed_sunday_when_naive_count_lands_on_it(self):
        svc = _make_svc(_hours_sun_closed(), tz="UTC")
        # checkout Mon 2026-05-25, 6-day loan → 2026-05-31 (Sunday, closed) → 2026-06-01 (Monday)
        checkout = datetime(2026, 5, 25, 10, 0, tzinfo=timezone.utc)
        due = svc.compute_due_at(checkout, 6)
        assert due == datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc)

    def test_rolls_past_holiday(self):
        closed = [ClosedDate(start_date=date(2026, 6, 8), end_date=date(2026, 6, 8), label="Test")]
        svc = _make_svc(_hours_all_open(), closed, tz="UTC")
        checkout = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
        due = svc.compute_due_at(checkout, 14)
        # 14 days = 2026-06-08 (closed) → rolls to 2026-06-09
        assert due == datetime(2026, 6, 9, 17, 0, tzinfo=timezone.utc)

    def test_no_open_times_falls_back_to_end_of_day(self):
        # is_open but no close_time set — fall back to 23:59
        hours = [
            LibraryHours(weekday=wd, is_open=True, open_time=None, close_time=None)
            for wd in range(7)
        ]
        svc = _make_svc(hours, tz="UTC")
        checkout = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
        due = svc.compute_due_at(checkout, 1)
        # 2026-05-26 at 23:59
        assert due == datetime(2026, 5, 26, 23, 59, tzinfo=timezone.utc)

    def test_local_timezone_converts_close_time_to_utc(self):
        # America/New_York is UTC-4 (EDT) in June
        # close time 17:00 local = 21:00 UTC
        svc = _make_svc(_hours_all_open(), tz="America/New_York")
        checkout = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
        due = svc.compute_due_at(checkout, 14)
        # 2026-06-08 17:00 EDT = 21:00 UTC
        assert due == datetime(2026, 6, 8, 21, 0, tzinfo=timezone.utc)

    def test_dst_spring_forward_2026(self):
        # 2026-03-08 is spring-forward night in America/New_York
        # UTC-5 before, UTC-4 after; 17:00 local on 2026-03-08 = 21:00 UTC (EDT)
        svc = _make_svc(_hours_all_open(), tz="America/New_York")
        checkout = datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc)  # 14 days before Mar 8
        due = svc.compute_due_at(checkout, 14)
        # 2026-03-08 17:00 EDT = 21:00 UTC
        assert due == datetime(2026, 3, 8, 21, 0, tzinfo=timezone.utc)

    def test_dst_fall_back_2026(self):
        # 2026-11-01 is fall-back in America/New_York (UTC-4 → UTC-5)
        # 17:00 EST on 2026-11-01 = 22:00 UTC
        svc = _make_svc(_hours_all_open(), tz="America/New_York")
        checkout = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)  # 14 days before Nov 1
        due = svc.compute_due_at(checkout, 14)
        # 2026-11-01 17:00 EST = 22:00 UTC
        assert due == datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# closed_days_between
# ---------------------------------------------------------------------------

class TestClosedDaysBetween:
    def test_no_closed_days(self):
        svc = _make_svc(_hours_all_open(), [])
        start = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 8, 0, 0, tzinfo=timezone.utc)
        assert svc.closed_days_between(start, end) == 0

    def test_sunday_closed_in_range(self):
        svc = _make_svc(_hours_sun_closed(), [], tz="UTC")
        # June 1–7 inclusive (7 days); June 7 is a Sunday (closed)
        start = datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
        # local dates June 2–8; June 7 (Sun) is in range
        assert svc.closed_days_between(start, end) == 1

    def test_holiday_counted(self):
        closed = [ClosedDate(start_date=date(2026, 6, 4), end_date=date(2026, 6, 4), label="Holiday")]
        svc = _make_svc(_hours_all_open(), closed, tz="UTC")
        start = datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
        assert svc.closed_days_between(start, end) == 1

    def test_sunday_plus_holiday(self):
        closed = [ClosedDate(start_date=date(2026, 6, 4), end_date=date(2026, 6, 4), label="Holiday")]
        svc = _make_svc(_hours_sun_closed(), closed, tz="UTC")
        # June 7 (Sun) + June 4 (Holiday Thu)
        start = datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
        assert svc.closed_days_between(start, end) == 2

    def test_same_start_end_returns_zero(self):
        svc = _make_svc(_hours_all_open(), [])
        ts = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        assert svc.closed_days_between(ts, ts) == 0

    def test_end_before_start_returns_zero(self):
        svc = _make_svc(_hours_all_open(), [])
        start = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        assert svc.closed_days_between(start, end) == 0

    def test_range_uses_local_dates(self):
        # closed_days_between uses half-open [start_local, end_local) aligned
        # with timedelta.days semantics.
        # America/New_York UTC-4 in summer.
        # start = 2026-06-05 23:00 UTC = June 5 19:00 EDT → local date June 5 (Fri)
        # end   = 2026-06-08 20:00 UTC = June 8 16:00 EDT → local date June 8 (Mon)
        # delta.days = 2; local range [June 5, June 8): June 5, June 6 (Sat), June 7 (Sun, closed)
        # → 1 closed day (June 7 Sunday)
        svc = _make_svc(_hours_sun_closed(), [], tz="America/New_York")
        start = datetime(2026, 6, 5, 23, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 8, 20, 0, tzinfo=timezone.utc)
        assert svc.closed_days_between(start, end) == 1
