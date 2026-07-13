"""--format coverage for the people group: patron, user, role, patron-category,
household.

Pattern mirrors ``tests/integration/test_cli_format_catalog.py``: patch
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


def test_patron_list_json(session):
    r = _invoke(
        session, "patron",
        ["patron", "add", "--name", "Ada Lovelace", "--email", "ada@example.com"],
    )
    assert r.exit_code == 0, r.output

    r = _invoke(session, "patron", ["patron", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert {"library_card_number", "full_name", "contact_email", "contact_phone",
            "is_active"} <= set(data[0])
    row = next(row for row in data if row["full_name"] == "Ada Lovelace")
    assert row["contact_email"] == "ada@example.com"
    assert row["is_active"] is True


def test_user_list_json(session):
    r = _invoke(
        session, "user",
        ["user", "add", "--username", "alice", "--password", "secret1234",
         "--role", "Librarian"],
    )
    assert r.exit_code == 0, r.output

    r = _invoke(session, "user", ["user", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert {"username", "role", "is_active"} <= set(data[0])
    row = next(row for row in data if row["username"] == "alice")
    assert row["role"] == "Librarian"
    assert row["is_active"] is True


def test_role_list_json(session):
    r = _invoke(session, "role", ["role", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert {"id", "name", "is_system", "permissions"} <= set(data[0])
    row = next(row for row in data if row["name"] == "Librarian")
    assert row["is_system"] is True


def test_role_list_json_permissions_is_list(session):
    r = _invoke(session, "role", ["role", "list", "--format", "json"])
    data = json.loads(r.stdout)
    assert isinstance(data[0]["permissions"], list)
    admin = next(row for row in data if row["name"] == "Administrator")
    assert admin["permissions"] == ["*"]


def test_patron_category_list_json(session):
    r = _invoke(session, "patron_category", ["patron-category", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert {"code", "display_name", "is_default"} <= set(data[0])
    row = next(row for row in data if row["code"] == "adult")
    assert row["display_name"] == "Adult"
    assert row["is_default"] is True


def test_household_list_and_show_json(session):
    r = _invoke(session, "household", ["household", "create", "--name", "Test HH"])
    assert r.exit_code == 0, r.output
    hh_id = int(r.output.strip().split()[2].rstrip(":"))

    r = _invoke(
        session, "patron",
        ["patron", "add", "--name", "Household Member"],
    )
    assert r.exit_code == 0, r.output
    card = next(
        line.split("Card number :")[1].strip()
        for line in r.output.splitlines() if "Card number" in line
    )

    r = _invoke(
        session, "household",
        ["household", "add-member", "--id", str(hh_id), "--card", card],
    )
    assert r.exit_code == 0, r.output

    r = _invoke(session, "household", ["household", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert {"id", "name", "member_count"} <= set(data[0])
    row = next(row for row in data if row["id"] == hh_id)
    assert row["member_count"] == 1

    r = _invoke(
        session, "household",
        ["household", "show", "--id", str(hh_id), "--format", "json"],
    )
    assert r.exit_code == 0, r.output
    obj = json.loads(r.stdout)
    assert {"id", "name", "notes", "members"} <= set(obj)
    assert obj["id"] == hh_id
    assert len(obj["members"]) == 1
    member = obj["members"][0]
    assert {"library_card_number", "full_name", "is_active", "loans", "holds"} <= set(member)
    assert member["library_card_number"] == card
    assert member["full_name"] == "Household Member"
