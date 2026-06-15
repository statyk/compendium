from __future__ import annotations

from typer.testing import CliRunner

from compendium.cli.main import app

runner = CliRunner()


def test_init_creates_bundle(tmp_path):
    target = tmp_path / "deploy"
    result = runner.invoke(app, ["init", str(target), "--admin-password", "pw"])
    assert result.exit_code == 0, result.output
    assert (target / "docker-compose.yml").is_file()
    assert (target / ".env").is_file()


def test_init_prints_generated_admin_password(tmp_path):
    target = tmp_path / "deploy"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.output
    assert "Admin" in result.output
    assert "password" in result.output.lower()


def test_init_conflict_without_force_errors(tmp_path):
    target = tmp_path / "deploy"
    runner.invoke(app, ["init", str(target), "--admin-password", "pw"])
    result = runner.invoke(app, ["init", str(target), "--admin-password", "pw"])
    assert result.exit_code == 1
    assert "force" in result.output.lower()


def test_init_help_warns_password_printed():
    result = runner.invoke(app, ["init", "--help"])
    assert result.exit_code == 0
    assert "print" in result.output.lower()
