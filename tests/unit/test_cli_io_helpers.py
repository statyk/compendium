import typer
import pytest
from typer.testing import CliRunner

from compendium.cli.io import error, register_alias, resolve_identifier, truncation_notice

runner = CliRunner()


def test_error_prints_red_prefix_to_stderr(capsys):
    error("boom")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error: boom" in captured.err


def test_register_alias_registers_hidden_command():
    app = typer.Typer()

    @app.command("edit")
    def edit_cmd():
        typer.echo("ran")

    register_alias(app, "rename", edit_cmd)
    result_new = runner.invoke(app, ["edit"])
    result_old = runner.invoke(app, ["rename"])
    assert result_new.exit_code == 0 and result_old.exit_code == 0
    assert result_new.output == result_old.output
    help_result = runner.invoke(app, ["--help"])
    assert "rename" not in help_result.output


def test_resolve_identifier_prefers_either_and_rejects_conflict():
    assert resolve_identifier("B1", None, label="barcode") == "B1"
    assert resolve_identifier(None, "B1", label="barcode") == "B1"
    assert resolve_identifier("B1", "B1", label="barcode") == "B1"
    with pytest.raises(typer.Exit) as exc_info:
        resolve_identifier("B1", "B2", label="barcode")
    assert exc_info.value.exit_code == 2
    with pytest.raises(typer.Exit) as exc_info:
        resolve_identifier(None, None, label="barcode")
    assert exc_info.value.exit_code == 2


def test_truncation_notice_only_fires_at_limit(capsys):
    truncation_notice(5, 10)
    assert capsys.readouterr().err == ""
    truncation_notice(10, 10)
    assert "Showing first 10 row(s)" in capsys.readouterr().err
