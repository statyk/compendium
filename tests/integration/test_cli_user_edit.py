"""user edit merges set-role and set-password; old commands stay as hidden aliases.

Follows the same pattern as tests/integration/test_cli_commands.py: patch
``session_scope`` in the command module under test to yield the shared
``session`` fixture from tests/conftest.py, then drive the CLI via
``typer.testing.CliRunner``.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from typer.testing import CliRunner

from compendium.cli.main import app

_MODULE = "compendium.cli.commands.user"


def _invoke(session, args: list[str], input: str | None = None, env: dict | None = None):
    @contextmanager
    def _scope():
        yield session

    runner = CliRunner()
    with patch(f"{_MODULE}.session_scope", _scope):
        return runner.invoke(app, args, input=input, env=env)


def test_user_edit_requires_a_change_flag(session):
    r = _invoke(session, ["user", "edit", "someone"])
    assert r.exit_code == 2
    assert "at least one of --role" in r.output


def test_user_edit_sets_password(session):
    _invoke(
        session,
        ["user", "add", "--username", "edituser1", "--password", "old12345", "--role", "Patron"],
    )
    r = _invoke(session, ["user", "edit", "edituser1", "--password", "newpass123"])
    assert r.exit_code == 0, r.output
    assert "Password reset" in r.output


def test_user_edit_sets_role_via_positional_username(session):
    _invoke(
        session,
        ["user", "add", "--username", "editadmin", "--password", "s", "--role", "Administrator"],
    )
    _invoke(
        session,
        ["user", "add", "--username", "edituser2", "--password", "s", "--role", "Patron"],
        env={"COMPENDIUM_ACTOR_USERNAME": "editadmin"},
    )
    r = _invoke(
        session,
        ["user", "edit", "edituser2", "--role", "Librarian"],
        env={"COMPENDIUM_ACTOR_USERNAME": "editadmin"},
    )
    assert r.exit_code == 0, r.output
    assert "role set to 'Librarian'" in r.output


def test_user_edit_role_without_actor_fails(session):
    _invoke(
        session,
        ["user", "add", "--username", "edituser3", "--password", "s", "--role", "Patron"],
    )
    r = _invoke(session, ["user", "edit", "edituser3", "--role", "Librarian"])
    assert r.exit_code == 1
    assert "COMPENDIUM_ACTOR_USERNAME" in r.output


def test_user_edit_prompt_password(session):
    _invoke(
        session,
        ["user", "add", "--username", "edituser4", "--password", "old12345", "--role", "Patron"],
    )
    r = _invoke(
        session,
        ["user", "edit", "edituser4", "--prompt-password"],
        input="promptedpass\npromptedpass\n",
    )
    assert r.exit_code == 0, r.output
    assert "Password reset" in r.output


def test_user_edit_username_conflict_via_legacy_option(session):
    r = _invoke(
        session,
        ["user", "edit", "someone", "--username", "someone-else", "--password", "x"],
    )
    assert r.exit_code == 2


def test_set_role_alias_still_works_and_is_hidden(session):
    _invoke(
        session,
        ["user", "add", "--username", "aliasadmin", "--password", "s", "--role", "Administrator"],
    )
    _invoke(
        session,
        ["user", "add", "--username", "aliasuser1", "--password", "s", "--role", "Patron"],
        env={"COMPENDIUM_ACTOR_USERNAME": "aliasadmin"},
    )
    r = _invoke(
        session,
        ["user", "set-role", "--username", "aliasuser1", "--role", "Librarian"],
        env={"COMPENDIUM_ACTOR_USERNAME": "aliasadmin"},
    )
    assert r.exit_code == 0, r.output
    assert "role set to 'Librarian'" in r.output

    help_out = _invoke(session, ["user", "--help"]).output
    assert "set-role" not in help_out


def test_set_password_alias_still_works_and_is_hidden(session):
    _invoke(
        session,
        ["user", "add", "--username", "aliasuser2", "--password", "old12345", "--role", "Patron"],
    )
    r = _invoke(
        session,
        ["user", "set-password", "--username", "aliasuser2", "--password", "new12345"],
    )
    assert r.exit_code == 0, r.output
    assert "Password reset" in r.output

    help_out = _invoke(session, ["user", "--help"]).output
    assert "set-password" not in help_out
