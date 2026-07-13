"""--format coverage for the reports group."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

from typer.testing import CliRunner

from compendium.cli.main import app


def _invoke(session, args):
    @contextmanager
    def _scope():
        yield session

    with patch("compendium.cli.commands.reports.session_scope", _scope):
        # NOTE: the installed click (8.3.x) removed CliRunner's mix_stderr
        # kwarg — stdout/stderr are separate streams by default now, which is
        # exactly what we want (result.stdout / result.stderr below).
        return CliRunner().invoke(app, args)


def test_checkouts_json_round_trips(session):
    res = _invoke(session, ["reports", "checkouts", "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert isinstance(data, list)
    if data:
        assert set(data[0]) == {"month", "count"}


def test_checkouts_csv_unchanged(session):
    res = _invoke(session, ["reports", "checkouts", "--format", "csv"])
    assert res.exit_code == 0
    assert res.stdout.splitlines()[0] == "month,count"


def test_checkouts_table_exits_zero(session):
    res = _invoke(session, ["reports", "checkouts"])
    assert res.exit_code == 0
    assert "Month" in res.stdout


def test_overdues_rejects_bad_format(session):
    res = _invoke(session, ["reports", "overdues", "--format", "yaml"])
    assert res.exit_code == 2
