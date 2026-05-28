"""Integration tests for calendar CLI commands."""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from compendium.cli.main import app
from compendium.repositories.sql.calendar_repository import (
    SqlClosedDateRepository,
    SqlLibraryHoursRepository,
)

runner = CliRunner()


def _patch_session(session):
    """Context manager that patches session_scope to yield the test session."""
    return patch(
        "compendium.cli.commands.calendar.session_scope",
        return_value=_session_ctx(session),
    )


class _session_ctx:
    def __init__(self, session):
        self._s = session

    def __enter__(self):
        return self._s

    def __exit__(self, *_):
        self._s.flush()


class TestCalendarCliHours:
    def test_hours_show(self, session):
        with _patch_session(session):
            result = runner.invoke(app, ["calendar", "hours", "show"])
        assert result.exit_code == 0
        assert "Monday" in result.output
        assert "UTC" in result.output  # default timezone

    def test_hours_set_closes_sunday(self, session):
        with _patch_session(session):
            result = runner.invoke(app, ["calendar", "hours", "set", "--weekday", "6", "--closed"])
        assert result.exit_code == 0
        row = SqlLibraryHoursRepository(session).get(6)
        assert row.is_open is False

    def test_hours_set_sets_close_time(self, session):
        with _patch_session(session):
            result = runner.invoke(
                app, ["calendar", "hours", "set", "--weekday", "0", "--open", "--close-time", "17:00"]
            )
        assert result.exit_code == 0
        row = SqlLibraryHoursRepository(session).get(0)
        assert row.close_time.strftime("%H:%M") == "17:00"


class TestCalendarCliClosedDates:
    def test_closed_date_list_empty(self, session):
        with _patch_session(session):
            result = runner.invoke(app, ["calendar", "closed-date", "list"])
        assert result.exit_code == 0
        assert "No closed dates" in result.output

    def test_closed_date_add_and_list(self, session):
        with _patch_session(session):
            result = runner.invoke(
                app,
                ["calendar", "closed-date", "add", "--start", "2026-12-25", "--label", "Christmas", "--annually"],
            )
        assert result.exit_code == 0

        with _patch_session(session):
            result = runner.invoke(app, ["calendar", "closed-date", "list"])
        assert "Christmas" in result.output
        assert "annually" in result.output.lower()

    def test_closed_date_delete(self, session):
        # Add via repo directly
        from compendium.domain.models import ClosedDate
        cd = ClosedDate(start_date=date(2026, 7, 4), end_date=date(2026, 7, 4), label="CLI Delete Test")
        session.add(cd)
        session.flush()
        cd_id = cd.id

        with _patch_session(session):
            result = runner.invoke(app, ["calendar", "closed-date", "delete", "--id", str(cd_id)])
        assert result.exit_code == 0
        assert SqlClosedDateRepository(session).get(cd_id) is None
