"""--format coverage for the admin group: branch, policy, calendar,
curated-list, labels.

Pattern mirrors ``tests/integration/test_cli_format_people.py``: patch
``session_scope`` in the command module under test to yield the shared
``session`` fixture, so the CLI runs against the same in-memory DB the rest of
the integration suite uses.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

from typer.testing import CliRunner

from compendium.cli.main import app


def _invoke(session, module: str, args: list[str]):
    @contextmanager
    def _scope():
        yield session

    runner = CliRunner()
    # NOTE: installed click (8.3.x) dropped CliRunner's mix_stderr kwarg —
    # stdout/stderr are separate streams by default now, which is exactly
    # what we want (result.stdout / result.stderr below).
    with patch(f"compendium.cli.commands.{module}.session_scope", _scope):
        return runner.invoke(app, args)


def test_branch_list_json(session):
    r = _invoke(session, "branch", ["branch", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert {"code", "name", "is_default", "classification_scheme"} <= set(data[0])
    row = next(row for row in data if row["code"] == "MAIN")
    assert row["is_default"] is True


def test_policy_list_json(session):
    r = _invoke(session, "policy", ["policy", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert {"id", "name", "media_type_id", "loan_period_days", "max_renewals",
            "is_default"} <= set(data[0])


def test_calendar_hours_show_json(session):
    r = _invoke(session, "calendar", ["calendar", "hours", "show", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert set(data) == {"timezone", "days"}
    assert len(data["days"]) == 7
    day = data["days"][0]
    assert {"weekday", "weekday_name", "is_open", "open_time", "close_time"} <= set(day)


def test_calendar_closed_date_list_json(session):
    r = _invoke(
        session, "calendar",
        ["calendar", "closed-date", "add", "--start", "2026-12-25", "--label", "Christmas"],
    )
    assert r.exit_code == 0, r.output

    r = _invoke(session, "calendar", ["calendar", "closed-date", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert {"id", "start_date", "end_date", "recurs_annually", "label"} <= set(data[0])
    row = next(row for row in data if row["label"] == "Christmas")
    assert row["start_date"] == "2026-12-25"


def test_curated_list_list_and_show_json(session):
    r = _invoke(
        session, "curated_list",
        ["curated-list", "create", "--name", "Staff Picks"],
    )
    assert r.exit_code == 0, r.output
    slug = r.output.strip().split("slug: ")[1].rstrip(")")

    r = _invoke(session, "curated_list", ["curated-list", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert {"slug", "name", "is_public", "is_featured", "entry_count"} <= set(data[0])
    row = next(row for row in data if row["slug"] == slug)
    assert row["name"] == "Staff Picks"

    r = _invoke(
        session, "curated_list",
        ["curated-list", "show", slug, "--format", "json"],
    )
    assert r.exit_code == 0, r.output
    obj = json.loads(r.stdout)
    assert {"slug", "name", "description", "is_public", "is_featured",
            "display_order", "entries"} <= set(obj)
    assert obj["slug"] == slug
    assert obj["entries"] == []


def test_labels_templates_json(session):
    r = _invoke(session, "labels", ["labels", "templates", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert {"key", "display", "per_sheet", "kinds"} <= set(data[0])
    row = next(row for row in data if row["key"] == "avery-5160")
    assert isinstance(row["kinds"], list)
