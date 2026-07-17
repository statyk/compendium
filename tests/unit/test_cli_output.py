"""Unit tests for the shared CLI output helpers."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import typer
from typer.testing import CliRunner

from compendium.cli.output import (
    Column,
    OutputFormat,
    emit_detail,
    emit_list,
    format_option,
    json_default,
)

ROWS = [
    {"id": 1, "title": "Dune", "due_at": datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)},
    {"id": 2, "title": "Emma", "due_at": None},
]
COLS = [
    Column("id", "ID", justify="right"),
    Column("title", "Title"),
    Column("due_at", "Due", formatter=lambda v: v.strftime("%Y-%m-%d") if v else "—"),
]


def test_emit_list_json_round_trips(capsys):
    emit_list(ROWS, COLS, "json")
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert err == ""
    assert data[0]["id"] == 1
    # full dict, ISO UTC datetime, null passthrough
    assert data[0]["due_at"] == "2026-07-01T12:00:00+00:00"
    assert data[1]["due_at"] is None


def test_emit_list_json_empty_is_empty_array(capsys):
    emit_list([], COLS, "json", empty="No loans.")
    out, err = capsys.readouterr()
    assert json.loads(out) == []
    assert "No loans." not in out


def test_emit_list_table_empty_message(capsys):
    emit_list([], COLS, "table", empty="No loans.")
    out, _ = capsys.readouterr()
    assert "No loans." in out


def test_emit_list_table_uses_formatter_and_headers(capsys):
    emit_list(ROWS, COLS, "table")
    out, _ = capsys.readouterr()
    assert "Title" in out and "Dune" in out
    assert "2026-07-01" in out  # formatter applied
    assert "12:00" not in out


def test_emit_detail_json_object(capsys):
    emit_detail({"key": "loan_period_days", "value": 21}, "json")
    out, _ = capsys.readouterr()
    assert json.loads(out) == {"key": "loan_period_days", "value": 21}


def test_emit_detail_table_labels(capsys):
    emit_detail({"barcode": "B-1", "status": "available"}, "table", title="Copy B-1")
    out, _ = capsys.readouterr()
    assert "Copy B-1" in out and "barcode" in out and "B-1" in out


def test_json_default_date_and_enum():
    assert json_default(date(2026, 7, 12)) == "2026-07-12"
    assert json_default(OutputFormat.JSON) == "json"


def test_json_default_naive_datetime_treated_as_utc():
    # No tzinfo: json_default assumes UTC rather than guessing local time.
    naive = datetime(2026, 7, 12, 9, 30, 0)
    assert json_default(naive) == "2026-07-12T09:30:00+00:00"


def test_json_default_non_utc_aware_datetime_converted_to_utc():
    # Aware, non-UTC offset: converted to UTC before serializing.
    eastern = timezone(timedelta(hours=-5))
    aware = datetime(2026, 7, 12, 9, 30, 0, tzinfo=eastern)
    assert json_default(aware) == "2026-07-12T14:30:00+00:00"


def _fmt_app(csv_ok: bool = False) -> typer.Typer:
    app = typer.Typer()

    @app.command()
    def cmd(format: str = format_option(csv_ok=csv_ok)) -> None:
        typer.echo(f"fmt={format}")

    return app


def test_format_option_default_and_case():
    runner = CliRunner()
    assert "fmt=table" in runner.invoke(_fmt_app(), []).output
    assert "fmt=json" in runner.invoke(_fmt_app(), ["--format", "JSON"]).output


def test_format_option_rejects_csv_unless_allowed():
    runner = CliRunner()
    res = runner.invoke(_fmt_app(), ["--format", "csv"])
    assert res.exit_code == 2
    res_ok = runner.invoke(_fmt_app(csv_ok=True), ["--format", "csv"])
    assert "fmt=csv" in res_ok.output


def test_format_option_rejects_garbage():
    res = CliRunner().invoke(_fmt_app(), ["--format", "yaml"])
    assert res.exit_code == 2
