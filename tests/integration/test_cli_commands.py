"""Smoke tests for CLI subcommands.

Goal: exercise every command's argument wiring and service call at least once
so broken flags / refactor drift fail loudly in CI. Business-logic correctness
lives in the service-layer tests — this file verifies the CLI surface itself.

Pattern: patch ``session_scope`` in the command module under test to yield the
shared test session from ``conftest``. The CLI then runs against the same
SQLite-in-memory DB every other integration test uses.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from compendium.cli.main import app
from compendium.domain.models import Patron
from compendium.repositories.sql.patron_repository import SqlPatronRepository


def _invoke(
    session,
    args: list[str],
    module_path: str,
    input: str | None = None,
    env: dict | None = None,
):
    """Run a CLI command with ``session_scope`` patched in the given module."""

    @contextmanager
    def _scope():
        yield session

    runner = CliRunner()
    with patch(f"{module_path}.session_scope", _scope):
        return runner.invoke(app, args, input=input, env=env)


# ──────────────────────────────────────────────────────────────────────────────
# db
# ──────────────────────────────────────────────────────────────────────────────


class TestDbCli:
    def test_history_lists_migrations(self, session):
        # history() is a simple alembic pass-through; just verify it doesn't crash.
        r = _invoke(session, ["db", "history"], "compendium.cli.commands.db")
        assert r.exit_code == 0


# ──────────────────────────────────────────────────────────────────────────────
# user
# ──────────────────────────────────────────────────────────────────────────────


class TestUserCli:
    def test_add_librarian(self, session):
        r = _invoke(
            session,
            ["user", "add", "--username", "alice", "--password", "secret1234", "--role", "Librarian"],
            "compendium.cli.commands.user",
        )
        assert r.exit_code == 0, r.output
        assert "alice" in r.output

    def test_add_unknown_role_fails(self, session):
        r = _invoke(
            session,
            ["user", "add", "--username", "bob", "--password", "s", "--role", "NoSuchRole"],
            "compendium.cli.commands.user",
        )
        assert r.exit_code == 1

    def test_list_empty(self, session):
        r = _invoke(session, ["user", "list"], "compendium.cli.commands.user")
        assert r.exit_code == 0
        assert "No users" in r.output or "alice" not in r.output

    def test_set_role_and_list(self, session):
        # Create an admin actor, then create charlie as Patron
        _invoke(
            session,
            ["user", "add", "--username", "admin", "--password", "s", "--role", "Administrator"],
            "compendium.cli.commands.user",
        )
        _invoke(
            session,
            ["user", "add", "--username", "charlie", "--password", "s", "--role", "Patron"],
            "compendium.cli.commands.user",
            env={"COMPENDIUM_ACTOR_USERNAME": "admin"},
        )
        r = _invoke(
            session,
            ["user", "set-role", "--username", "charlie", "--role", "Librarian"],
            "compendium.cli.commands.user",
            env={"COMPENDIUM_ACTOR_USERNAME": "admin"},
        )
        assert r.exit_code == 0, r.output
        r = _invoke(session, ["user", "list"], "compendium.cli.commands.user")
        assert "charlie" in r.output
        assert "Librarian" in r.output

    def test_set_role_without_actor_fails(self, session):
        _invoke(
            session,
            ["user", "add", "--username", "diane", "--password", "s", "--role", "Patron"],
            "compendium.cli.commands.user",
        )
        r = _invoke(
            session,
            ["user", "set-role", "--username", "diane", "--role", "Librarian"],
            "compendium.cli.commands.user",
        )
        assert r.exit_code == 1
        assert "COMPENDIUM_ACTOR_USERNAME" in r.output

    def test_set_password(self, session):
        _invoke(
            session,
            ["user", "add", "--username", "dave", "--password", "old12345", "--role", "Patron"],
            "compendium.cli.commands.user",
        )
        r = _invoke(
            session,
            ["user", "set-password", "--username", "dave", "--password", "new12345"],
            "compendium.cli.commands.user",
        )
        assert r.exit_code == 0, r.output

    def test_deactivate_then_list_shows_inactive(self, session):
        _invoke(
            session,
            ["user", "add", "--username", "eve", "--password", "s", "--role", "Patron"],
            "compendium.cli.commands.user",
        )
        r = _invoke(
            session,
            ["user", "deactivate", "--username", "eve"],
            "compendium.cli.commands.user",
        )
        assert r.exit_code == 0
        r = _invoke(session, ["user", "list", "--include-inactive"], "compendium.cli.commands.user")
        assert "inactive" in r.output


# ──────────────────────────────────────────────────────────────────────────────
# patron
# ──────────────────────────────────────────────────────────────────────────────


class TestPatronCli:
    def test_add(self, session):
        r = _invoke(
            session,
            ["patron", "add", "--name", "Ada Lovelace", "--email", "ada@example.com"],
            "compendium.cli.commands.patron",
        )
        assert r.exit_code == 0, r.output
        assert "Ada Lovelace" in r.output
        assert "Card number" in r.output

    def test_list_shows_patron(self, session):
        _invoke(
            session,
            ["patron", "add", "--name", "Ada Lovelace"],
            "compendium.cli.commands.patron",
        )
        r = _invoke(session, ["patron", "list"], "compendium.cli.commands.patron")
        assert r.exit_code == 0
        assert "Ada Lovelace" in r.output

    def test_list_search_filters(self, session):
        for name in ("Greta Findme", "Hank Skipme"):
            _invoke(
                session,
                ["patron", "add", "--name", name],
                "compendium.cli.commands.patron",
            )
        r = _invoke(
            session,
            ["patron", "list", "--search", "findme"],
            "compendium.cli.commands.patron",
        )
        assert r.exit_code == 0
        assert "Greta Findme" in r.output
        assert "Hank Skipme" not in r.output

    def test_list_search_no_match_message(self, session):
        r = _invoke(
            session,
            ["patron", "list", "--search", "zzz-no-such"],
            "compendium.cli.commands.patron",
        )
        assert r.exit_code == 0
        assert "No patrons match." in r.output

    def test_add_with_unknown_link_user_fails(self, session):
        r = _invoke(
            session,
            ["patron", "add", "--name", "Bob", "--link-user", "nonexistent"],
            "compendium.cli.commands.patron",
        )
        assert r.exit_code == 1
        assert "No user" in r.output

    def test_deactivate(self, session):
        r = _invoke(
            session,
            ["patron", "add", "--name", "Carol"],
            "compendium.cli.commands.patron",
        )
        assert r.exit_code == 0
        # extract card number
        card = next(
            line.split(":")[1].strip() for line in r.output.splitlines()
            if "Card number" in line
        )
        r = _invoke(
            session,
            ["patron", "deactivate", "--card", card],
            "compendium.cli.commands.patron",
        )
        assert r.exit_code == 0
        assert "Deactivated" in r.output

    def test_link_and_unlink_user(self, session):
        _invoke(
            session,
            ["user", "add", "--username", "dan", "--password", "s", "--role", "Patron"],
            "compendium.cli.commands.user",
        )
        r = _invoke(
            session,
            ["patron", "add", "--name", "Dan"],
            "compendium.cli.commands.patron",
        )
        card = next(
            line.split(":")[1].strip() for line in r.output.splitlines()
            if "Card number" in line
        )
        r = _invoke(
            session,
            ["patron", "link-user", "--card", card, "--username", "dan"],
            "compendium.cli.commands.patron",
        )
        assert r.exit_code == 0
        assert "Linked" in r.output
        r = _invoke(
            session,
            ["patron", "unlink-user", "--card", card],
            "compendium.cli.commands.patron",
        )
        assert r.exit_code == 0
        assert "Unlinked" in r.output

    def test_deactivate_unknown_card_fails(self, session):
        r = _invoke(
            session,
            ["patron", "deactivate", "--card", "NOPE"],
            "compendium.cli.commands.patron",
        )
        assert r.exit_code == 1


# ──────────────────────────────────────────────────────────────────────────────
# role
# ──────────────────────────────────────────────────────────────────────────────


class TestRoleCli:
    def test_list_preset_roles(self, session):
        r = _invoke(session, ["role", "list"], "compendium.cli.commands.role")
        assert r.exit_code == 0
        assert "Librarian" in r.output
        assert "Patron" in r.output

    def test_create_and_update_and_clone(self, session):
        r = _invoke(
            session,
            ["role", "create", "--name", "Curator", "--permissions", "item.view,item.edit"],
            "compendium.cli.commands.role",
        )
        assert r.exit_code == 0, r.output
        r = _invoke(session, ["role", "list"], "compendium.cli.commands.role")
        assert "Curator" in r.output
        # Table rendering doesn't expose a scrapeable "#<id>" prefix anymore;
        # use --format json to reliably extract the new role's id.
        r = _invoke(
            session, ["role", "list", "--format", "json"], "compendium.cli.commands.role"
        )
        curator_id = str(
            next(row["id"] for row in json.loads(r.stdout) if row["name"] == "Curator")
        )
        r = _invoke(
            session,
            ["role", "update", "--id", curator_id, "--full-access"],
            "compendium.cli.commands.role",
        )
        assert r.exit_code == 0, r.output
        r = _invoke(
            session,
            ["role", "clone", "--id", curator_id, "--name", "Curator2"],
            "compendium.cli.commands.role",
        )
        assert r.exit_code == 0, r.output

    def test_update_requires_some_change(self, session):
        # No name / permissions / full-access flag → fails.
        r = _invoke(
            session,
            ["role", "update", "--id", "1"],
            "compendium.cli.commands.role",
        )
        assert r.exit_code == 1

    def test_update_preset_rejected(self, session):
        # Librarian is a preset role (seeded), id 1 typically. Table rendering
        # doesn't expose a scrapeable "#<id>" prefix anymore; use --format
        # json to reliably extract the id.
        r = _invoke(
            session, ["role", "list", "--format", "json"], "compendium.cli.commands.role"
        )
        librarian_id = str(
            next(row["id"] for row in json.loads(r.stdout) if row["name"] == "Librarian")
        )
        r = _invoke(
            session,
            ["role", "update", "--id", librarian_id, "--name", "Boss"],
            "compendium.cli.commands.role",
        )
        assert r.exit_code == 1


# ──────────────────────────────────────────────────────────────────────────────
# branch
# ──────────────────────────────────────────────────────────────────────────────


class TestBranchCli:
    def test_list_shows_default_branch(self, session):
        # Migrated to the shared table/JSON output helper (Task 7): the
        # bracketed "[default]" marker is gone from the rich table; the
        # lowercase "default" cell text (matching the patron-category/role
        # list convention) is the new source of truth.
        r = _invoke(session, ["branch", "list"], "compendium.cli.commands.branch")
        assert r.exit_code == 0
        assert "default" in r.output

    def test_set_classification_scheme(self, session):
        # Migrated to the shared table/JSON output helper (Task 7): use
        # --format json to reliably pick a branch code instead of parsing
        # the rich table's column-aligned text.
        r = _invoke(
            session, ["branch", "list", "--format", "json"], "compendium.cli.commands.branch"
        )
        data = json.loads(r.stdout)
        code = next(row["code"] for row in data if row["is_default"])
        r = _invoke(
            session,
            ["branch", "set", "--code", code, "--classification", "ddc"],
            "compendium.cli.commands.branch",
        )
        assert r.exit_code == 0
        assert "DDC" in r.output

    def test_set_invalid_scheme_rejected(self, session):
        r = _invoke(
            session,
            ["branch", "set", "--code", "MAIN", "--classification", "bogus"],
            "compendium.cli.commands.branch",
        )
        assert r.exit_code == 1
        assert "invalid scheme" in r.output

    def test_set_unknown_branch_fails(self, session):
        r = _invoke(
            session,
            ["branch", "set", "--code", "NOPE", "--classification", "lcc"],
            "compendium.cli.commands.branch",
        )
        assert r.exit_code == 1

    def test_branch_set_name(self, session):
        from compendium.repositories.sql.branch_repository import SqlBranchRepository

        r = _invoke(
            session,
            ["branch", "set", "--code", "MAIN", "--name", "Annex"],
            "compendium.cli.commands.branch",
        )
        assert r.exit_code == 0
        assert SqlBranchRepository(session).get_by_code("MAIN").name == "Annex"


# ──────────────────────────────────────────────────────────────────────────────
# audit
# ──────────────────────────────────────────────────────────────────────────────


class TestAuditCli:
    def test_list_empty(self, session):
        r = _invoke(session, ["audit", "list"], "compendium.cli.commands.audit")
        assert r.exit_code == 0
        assert "No audit" in r.output

    def test_list_with_filter_runs(self, session):
        # Trigger an auditable action first.
        _invoke(
            session,
            ["user", "add", "--username", "frank", "--password", "s", "--role", "Patron"],
            "compendium.cli.commands.user",
        )
        r = _invoke(
            session,
            ["audit", "list", "--entity", "user", "--limit", "5"],
            "compendium.cli.commands.audit",
        )
        assert r.exit_code == 0
        # Either header shows or "No audit" — both fine, we just want no crash.

    def test_details_are_compact_json(self, session):
        from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
        from compendium.services.audit import AuditService

        AuditService(SqlAuditLogRepository(session)).record(
            actor=None,
            actor_label="cli:test",
            source="cli",
            entity_type="work",
            entity_id=1,
            action="update",
            details={"field": "title", "to": "New"},
        )
        session.flush()

        # Widen the Rich console (via COLUMNS) so the Details cell isn't
        # ellipsis-truncated in the default 80-col fallback.
        r = _invoke(
            session,
            ["audit", "list"],
            "compendium.cli.commands.audit",
            env={"COLUMNS": "200"},
        )
        assert r.exit_code == 0
        assert '{"field":"title","to":"New"}' in r.output
        assert "{'field'" not in r.output

        r_json = _invoke(session, ["audit", "list", "--format", "json"], "compendium.cli.commands.audit")
        assert r_json.exit_code == 0
        payload = json.loads(r_json.output)
        assert payload[0]["details"] == {"field": "title", "to": "New"}


# ──────────────────────────────────────────────────────────────────────────────
# work + creator (needs seeded work with one item)
# ──────────────────────────────────────────────────────────────────────────────


def _seed_work(session):
    """Add a sample work via the catalog service directly (avoids HTTP stub)."""
    from unittest.mock import patch as _p

    from compendium.repositories.sql.branch_repository import SqlBranchRepository
    from compendium.repositories.sql.creator_repository import SqlCreatorRepository
    from compendium.repositories.sql.item_repository import SqlItemRepository
    from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
    from compendium.repositories.sql.work_repository import SqlWorkRepository
    from compendium.services.catalog import CatalogService

    with _p(
        "compendium.services.metadata.lookup_isbn",
        return_value={
            "title": "Dune",
            "authors": [{"name": "Frank Herbert"}],
            "publishers": [{"name": "Chilton"}],
            "publish_date": "1965",
            "cover": {},
            "identifiers": {},
        },
    ):
        catalog = CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        )
        work, item = catalog.add_from_isbn("9780441013593")
    session.flush()
    return work, item


class TestWorkCli:
    def test_search_no_results(self, session):
        r = _invoke(
            session,
            ["work", "search", "zzzzz-no-match"],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 0
        assert "No results" in r.output

    def test_show_and_search_find_seeded_work(self, session):
        work, _ = _seed_work(session)
        r = _invoke(session, ["work", "show", str(work.id)], "compendium.cli.commands.work")
        assert r.exit_code == 0
        assert "Dune" in r.output
        assert "Frank Herbert" in r.output
        r = _invoke(session, ["work", "search", "Dune"], "compendium.cli.commands.work")
        assert r.exit_code == 0
        assert "Dune" in r.output

    def test_search_with_media_filter(self, session):
        _seed_work(session)  # Dune is a book
        r = _invoke(
            session,
            ["work", "search", "Dune", "--media-type", "dvd"],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 0
        assert "No results" in r.output

    def test_search_with_decade_filter(self, session):
        _seed_work(session)  # Dune is 1965 → 1960s
        r = _invoke(
            session,
            ["work", "search", "Dune", "--decade", "2010"],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 0
        assert "No results" in r.output

    def test_new_arrivals(self, session):
        _seed_work(session)
        r = _invoke(
            session,
            ["work", "new-arrivals", "--days", "30", "--limit", "10"],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 0
        # Either the seeded work appears, or the empty message — both valid.
        assert "Dune" in r.output or "No works added" in r.output

    def test_recently_returned_empty(self, session):
        r = _invoke(
            session,
            ["work", "recently-returned"],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 0
        assert "No works returned" in r.output

    def test_show_unknown_fails(self, session):
        r = _invoke(session, ["work", "show", "999999"], "compendium.cli.commands.work")
        assert r.exit_code == 1

    def test_edit_requires_exactly_one_id(self, session):
        r = _invoke(
            session,
            ["work", "edit", "--title", "X"],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 1

    def test_edit_work_updates_fields(self, session):
        work, _ = _seed_work(session)
        r = _invoke(
            session,
            ["work", "edit", "--work-id", str(work.id), "--subtitle", "The Sci-Fi Classic", "--year", "1965"],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 0
        assert "The Sci-Fi Classic" in r.output

    def test_edit_without_field_flags_fails(self, session):
        work, _ = _seed_work(session)
        r = _invoke(
            session,
            ["work", "edit", "--work-id", str(work.id)],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 1

    def test_edit_unknown_work_fails(self, session):
        r = _invoke(
            session,
            ["work", "edit", "--work-id", "999999", "--title", "X"],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 1

    def test_creator_add_remove_set_order(self, session):
        work, _ = _seed_work(session)
        r = _invoke(
            session,
            ["work", "creator", "add", "--work-id", str(work.id), "--name", "Brian Herbert", "--role", "author"],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 0, r.output
        assert "Brian Herbert" in r.output
        r = _invoke(
            session,
            ["work", "creator", "move", "--work-id", str(work.id),
             "--name", "Brian Herbert", "--role", "author", "--position", "0"],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 0
        r = _invoke(
            session,
            ["work", "creator", "remove", "--work-id", str(work.id),
             "--name", "Brian Herbert", "--role", "author"],
            "compendium.cli.commands.work",
        )
        assert r.exit_code == 0
        assert "Removed" in r.output


# ──────────────────────────────────────────────────────────────────────────────
# creator rename
# ──────────────────────────────────────────────────────────────────────────────


class TestCreatorCli:
    def test_rename_unknown_fails(self, session):
        r = _invoke(
            session,
            ["creator", "rename", "--id", "999999", "--name", "Anyone"],
            "compendium.cli.commands.creator",
        )
        assert r.exit_code == 1

    def test_rename_seeded_creator(self, session):
        _seed_work(session)  # creates creator "Frank Herbert"
        from compendium.repositories.sql.creator_repository import SqlCreatorRepository
        creator = SqlCreatorRepository(session).get_by_sort_name("Herbert, Frank")
        assert creator is not None
        r = _invoke(
            session,
            ["creator", "rename", "--id", str(creator.id), "--name", "F. Herbert"],
            "compendium.cli.commands.creator",
        )
        assert r.exit_code == 0
        assert "F. Herbert" in r.output


# ──────────────────────────────────────────────────────────────────────────────
# hold
# ──────────────────────────────────────────────────────────────────────────────


class TestHoldCli:
    def test_place_list_cancel(self, session):
        work, _ = _seed_work(session)
        patron = Patron(library_card_number="CLIH001", full_name="Hold Patron")
        SqlPatronRepository(session).add(patron)
        session.flush()

        r = _invoke(
            session,
            ["hold", "place", "--work-id", str(work.id), "--card", "CLIH001"],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 0, r.output
        assert "Hold placed" in r.output
        # With immediate-promote in place, status is "available" since the
        # seeded work has an AVAILABLE copy.
        assert "available" in r.output

        # Extract hold id straight from the `place` confirmation (format
        # "  Hold ID : <id>") rather than parsing the `list` table — table
        # cells can be truncated to fit the (narrow, non-tty) console width.
        hold_id = next(
            line.split(":", 1)[1].strip()
            for line in r.output.splitlines()
            if line.strip().startswith("Hold ID")
        )

        r = _invoke(
            session,
            ["hold", "list", "--card", "CLIH001"],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 0
        assert "Status" in r.output

        r = _invoke(
            session,
            ["hold", "cancel", "--id", hold_id, "--card", "CLIH001"],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 0
        assert "cancelled" in r.output

    def test_list_unknown_patron_fails(self, session):
        r = _invoke(
            session,
            ["hold", "list", "--card", "NOCARD"],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 1

    def test_list_no_holds(self, session):
        patron = Patron(library_card_number="CLIH002", full_name="Empty")
        SqlPatronRepository(session).add(patron)
        session.flush()
        r = _invoke(
            session,
            ["hold", "list", "--card", "CLIH002"],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 0
        assert "no active holds" in r.output

    def test_cancel_unknown_patron_fails(self, session):
        r = _invoke(
            session,
            ["hold", "cancel", "--id", "9999", "--card", "NOCARD"],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 1


# ──────────────────────────────────────────────────────────────────────────────
# CLI root / serve — lightweight surface checks
# ──────────────────────────────────────────────────────────────────────────────


class TestMainCli:
    def test_root_help_lists_subcommands(self):
        r = CliRunner().invoke(app, ["--help"])
        assert r.exit_code == 0
        for sub in ("item", "patron", "loan", "hold", "user", "role", "work"):
            assert sub in r.output

    def test_serve_help(self):
        r = CliRunner().invoke(app, ["serve", "--help"])
        assert r.exit_code == 0
        assert "host" in r.output.lower()


# ──────────────────────────────────────────────────────────────────────────────
# item — extends 14% baseline
# ──────────────────────────────────────────────────────────────────────────────


class TestItemCli:
    def test_list_empty(self, session):
        # `item list` is a hidden alias for `work list` (Task 5); the command
        # body lives in compendium.cli.commands.work, so session_scope must be
        # patched there for the alias invocation to hit the test DB.
        r = _invoke(session, ["item", "list"], "compendium.cli.commands.work")
        assert r.exit_code == 0
        assert "No works" in r.output

    def test_list_shows_seeded_work(self, session):
        _seed_work(session)
        r = _invoke(session, ["item", "list"], "compendium.cli.commands.work")
        assert r.exit_code == 0
        assert "Dune" in r.output

    def test_show_by_barcode(self, session):
        _, item = _seed_work(session)
        r = _invoke(
            session,
            ["item", "show", item.barcode],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 0
        assert "Dune" in r.output
        assert item.barcode in r.output

    def test_show_unknown_fails(self, session):
        r = _invoke(
            session,
            ["item", "show", "NOPE"],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 1

    def test_add_manual_book(self, session):
        r = _invoke(
            session,
            [
                "item", "add-manual",
                "--title", "Hand-Entered Book",
                "--author", "Anon",
                "--media-type", "book",
                "--year", "2024",
                "--isbn", "0000000000001",
                "--call-number", "CAL.123",
            ],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 0, r.output
        assert "Hand-Entered Book" in r.output
        assert "CAL.123" in r.output

    def test_add_requires_identifier(self, session):
        r = _invoke(
            session,
            ["item", "add"],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 1
        assert "provide" in r.output.lower()

    def test_add_upc_requires_media_type(self, session):
        r = _invoke(
            session,
            ["item", "add", "--upc", "012345678905"],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 1
        assert "media-type is required" in r.output

    def test_add_tmdb_requires_media_type(self, session):
        r = _invoke(
            session,
            ["item", "add", "--tmdb-id", "12345"],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 1

    def test_add_title_requires_media_type(self, session):
        r = _invoke(
            session,
            ["item", "add", "--title", "Something"],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 1

    def test_add_title_rejects_unsupported_media_type(self, session):
        # Pass a made-up media_type that's not in _TITLE_SEARCH_SOURCES.
        r = _invoke(
            session,
            ["item", "add", "--title", "X", "--media-type", "laserdisc"],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 1
        assert "not supported" in r.output

    def test_add_isbn_with_mocked_lookup(self, session):
        with patch(
            "compendium.services.metadata.lookup_isbn",
            return_value={
                "title": "The Two Towers",
                "authors": [{"name": "J.R.R. Tolkien"}],
                "publishers": [{"name": "Houghton Mifflin"}],
                "publish_date": "1954",
                "cover": {},
                "identifiers": {},
            },
        ):
            r = _invoke(
                session,
                ["item", "add", "--isbn", "9780618346257"],
                "compendium.cli.commands.item",
            )
        assert r.exit_code == 0, r.output
        assert "The Two Towers" in r.output
        assert "Tolkien" in r.output

    def test_edit_requires_a_field(self, session):
        _, item = _seed_work(session)
        r = _invoke(
            session,
            ["item", "edit", "--barcode", item.barcode],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 1

    def test_edit_updates_fields(self, session):
        _, item = _seed_work(session)
        r = _invoke(
            session,
            [
                "item", "edit",
                "--barcode", item.barcode,
                "--location", "Shelf A1",
                "--call-number", "FIC HER",
                "--condition", "good",
                "--notes", "gift from J.",
            ],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 0, r.output
        assert "Shelf A1" in r.output
        assert "FIC HER" in r.output

    def test_withdraw(self, session):
        _, item = _seed_work(session)
        r = _invoke(
            session,
            ["item", "withdraw", "--barcode", item.barcode],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 0
        assert "Withdrawn" in r.output

    def test_withdraw_unknown_fails(self, session):
        r = _invoke(
            session,
            ["item", "withdraw", "--barcode", "NOPE"],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 1

    def test_set_loanable_yes_and_no(self, session):
        _, item = _seed_work(session)
        r = _invoke(
            session,
            ["item", "set-loanable", "--barcode", item.barcode, "--no", "--reason", "reference"],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 0, r.output
        assert "no" in r.output.lower()
        r = _invoke(
            session,
            ["item", "set-loanable", "--barcode", item.barcode, "--yes"],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 0

    def test_set_loanable_conflicting_flags_fails(self, session):
        _, item = _seed_work(session)
        r = _invoke(
            session,
            ["item", "set-loanable", "--barcode", item.barcode, "--yes", "--no"],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 1

    def test_set_loanable_requires_a_flag(self, session):
        _, item = _seed_work(session)
        r = _invoke(
            session,
            ["item", "set-loanable", "--barcode", item.barcode],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 1


# ──────────────────────────────────────────────────────────────────────────────
# loan — extends 44% baseline (queue-override already covered in test_holds.py)
# ──────────────────────────────────────────────────────────────────────────────


class TestLoanCli:
    def test_checkout_checkin_lifecycle(self, session):
        _, item = _seed_work(session)
        patron = Patron(library_card_number="CLIL001", full_name="Loan Patron")
        SqlPatronRepository(session).add(patron)
        session.flush()

        r = _invoke(
            session,
            ["loan", "checkout", "--barcode", item.barcode, "--card", "CLIL001"],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 0, r.output
        assert "Checked out" in r.output

        r = _invoke(
            session,
            ["loan", "active", "--card", "CLIL001"],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 0
        assert "Due" in r.output

        r = _invoke(
            session,
            ["loan", "renew", "--barcode", item.barcode, "--card", "CLIL001"],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 0
        assert "Renewed" in r.output

        r = _invoke(
            session,
            ["loan", "checkin", "--barcode", item.barcode],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 0
        assert "Checked in" in r.output

    def test_renew_without_card_by_barcode_alone(self, session):
        _, item = _seed_work(session)
        patron = Patron(library_card_number="CLIL002", full_name="Cardless Renew Patron")
        SqlPatronRepository(session).add(patron)
        session.flush()

        r = _invoke(
            session,
            ["loan", "checkout", "--barcode", item.barcode, "--card", "CLIL002"],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 0, r.output

        r = _invoke(
            session,
            ["loan", "renew", "--barcode", item.barcode],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 0, r.output
        assert "Renewed" in r.output

    def test_checkout_unknown_item_fails(self, session):
        patron = Patron(library_card_number="CLIL002", full_name="X")
        SqlPatronRepository(session).add(patron)
        session.flush()
        r = _invoke(
            session,
            ["loan", "checkout", "--barcode", "NOPE", "--card", "CLIL002"],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 1

    def test_active_unknown_patron_fails(self, session):
        r = _invoke(
            session,
            ["loan", "active", "--card", "NOPE"],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 1

    def test_active_no_loans(self, session):
        patron = Patron(library_card_number="CLIL003", full_name="Idle")
        SqlPatronRepository(session).add(patron)
        session.flush()
        r = _invoke(
            session,
            ["loan", "active", "--card", "CLIL003"],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 0
        assert "no active loans" in r.output

    def test_declare_lost_then_clear(self, session):
        _, item = _seed_work(session)
        patron = Patron(library_card_number="CLIL004", full_name="LostPatron")
        SqlPatronRepository(session).add(patron)
        session.flush()
        # Must have been checked out to declare lost.
        _invoke(
            session,
            ["loan", "checkout", "--barcode", item.barcode, "--card", "CLIL004"],
            "compendium.cli.commands.loan",
        )
        r = _invoke(
            session,
            ["item", "declare-lost", "--barcode", item.barcode, "--replacement-cost-cents", "2500"],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 0, r.output
        assert "declared lost" in r.output
        r = _invoke(
            session,
            ["item", "clear-lost", "--barcode", item.barcode],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 0
        assert "recovered" in r.output

    def test_checkin_by_isbn_ambiguous_lists_candidates(self, session):
        from compendium.domain.models import Item, MediaType, Work
        from compendium.repositories.sql.branch_repository import SqlBranchRepository
        from compendium.repositories.sql.item_repository import SqlItemRepository
        from compendium.repositories.sql.work_repository import SqlWorkRepository

        book = session.query(MediaType).filter_by(code="book").one()
        w = Work(title="Dune CLI", media_type_id=book.id, isbn="9780553382563")
        SqlWorkRepository(session).add(w)
        session.flush()
        branch = SqlBranchRepository(session).get_default()
        for n, card in ((1, "CLIAMB01"), (2, "CLIAMB02")):
            item = Item(
                work_id=w.id,
                branch_id=branch.id,
                barcode=f"CLIAMB-{n}",
                accession_number=f"CLIAMB-A{n}",
            )
            SqlItemRepository(session).add(item)
            p = Patron(library_card_number=card, full_name=f"Cli Patron {n}")
            SqlPatronRepository(session).add(p)
            session.flush()
            r = _invoke(
                session,
                ["loan", "checkout", "--barcode", f"CLIAMB-{n}", "--card", card],
                "compendium.cli.commands.loan",
            )
            assert r.exit_code == 0, r.output
        r = _invoke(
            session,
            ["loan", "checkin", "--barcode", "9780553382563"],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 1
        assert "CLIAMB-1" in r.output
        assert "CLIAMB-2" in r.output
        assert "CLIAMB01" in r.output
        assert "Re-run with the copy's --barcode." in r.output

    def test_checkout_by_isbn_resolves_to_available_copy(self, session):
        from compendium.domain.models import Item, MediaType, Work
        from compendium.repositories.sql.branch_repository import SqlBranchRepository
        from compendium.repositories.sql.item_repository import SqlItemRepository
        from compendium.repositories.sql.work_repository import SqlWorkRepository

        book = session.query(MediaType).filter_by(code="book").one()
        w = Work(title="Dune ISBN Checkout", media_type_id=book.id, isbn="9780441013593")
        SqlWorkRepository(session).add(w)
        session.flush()
        branch = SqlBranchRepository(session).get_default()
        item = Item(
            work_id=w.id,
            branch_id=branch.id,
            barcode="CLIISBN-1",
            accession_number="CLIISBN-A1",
        )
        SqlItemRepository(session).add(item)
        patron = Patron(library_card_number="CLIISBN01", full_name="ISBN Checkout Patron")
        SqlPatronRepository(session).add(patron)
        session.flush()

        r = _invoke(
            session,
            ["loan", "checkout", "--barcode", "9780441013593", "--card", "CLIISBN01"],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 0, r.output
        assert "CLIISBN-1" in r.output

    def test_mark_damaged_then_clear(self, session):
        _, item = _seed_work(session)
        patron = Patron(library_card_number="CLIL005", full_name="DamagePatron")
        SqlPatronRepository(session).add(patron)
        session.flush()
        _invoke(
            session,
            ["loan", "checkout", "--barcode", item.barcode, "--card", "CLIL005"],
            "compendium.cli.commands.loan",
        )
        r = _invoke(
            session,
            [
                "item", "mark-damaged",
                "--barcode", item.barcode,
                "--amount-cents", "500",
                "--note", "cover torn",
            ],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 0, r.output
        assert "damaged" in r.output
        r = _invoke(
            session,
            ["item", "clear-damage", "--barcode", item.barcode],
            "compendium.cli.commands.item",
        )
        assert r.exit_code == 0


# ──────────────────────────────────────────────────────────────────────────────
# policy — extends 52% baseline
# ──────────────────────────────────────────────────────────────────────────────


class TestPolicyCli:
    def test_list_default_policy_seeded(self, session):
        # Migrated to the shared table/JSON output helper (Task 7): the
        # bracketed "[DEFAULT]" marker is gone from the rich table; the
        # lowercase "default" cell text (matching the patron-category/role
        # list convention) is the new source of truth.
        r = _invoke(session, ["policy", "list"], "compendium.cli.commands.policy")
        assert r.exit_code == 0
        assert "default" in r.output

    def test_create_and_set_fields(self, session):
        r = _invoke(
            session,
            ["policy", "create", "--name", "Fast", "--loan-days", "7", "--max-renewals", "0"],
            "compendium.cli.commands.policy",
        )
        assert r.exit_code == 0, r.output
        assert "Fast" in r.output
        # Grab id from list
        r = _invoke(session, ["policy", "list"], "compendium.cli.commands.policy")
        fast_id = next(
            line.strip().split()[0].lstrip("#")
            for line in r.output.splitlines()
            if "Fast" in line
        )
        r = _invoke(
            session,
            [
                "policy", "set",
                "--id", fast_id,
                "--loan-days", "10",
                "--max-renewals", "1",
                "--overdue-per-day-cents", "25",
                "--grace-days", "2",
                "--lost-default-cents", "1500",
                "--lost-processing-cents", "200",
            ],
            "compendium.cli.commands.policy",
        )
        assert r.exit_code == 0, r.output

    def test_set_requires_a_flag(self, session):
        r = _invoke(
            session,
            ["policy", "set", "--id", "1"],
            "compendium.cli.commands.policy",
        )
        assert r.exit_code == 1

    def test_delete_requires_yes_or_confirmation(self, session):
        r = _invoke(
            session,
            ["policy", "create", "--name", "Deletable", "--loan-days", "5"],
            "compendium.cli.commands.policy",
        )
        assert r.exit_code == 0, r.output
        r = _invoke(session, ["policy", "list"], "compendium.cli.commands.policy")
        pid = next(
            line.strip().split()[0].lstrip("#")
            for line in r.output.splitlines()
            if "Deletable" in line
        )

        r = _invoke(
            session,
            ["policy", "delete", "--id", pid],
            "compendium.cli.commands.policy",
            input="n\n",
        )
        assert r.exit_code == 1

        r = _invoke(
            session,
            ["policy", "delete", "--id", pid, "--yes"],
            "compendium.cli.commands.policy",
        )
        assert r.exit_code == 0, r.output
        assert "deleted" in r.output.lower()


# ──────────────────────────────────────────────────────────────────────────────
# patron-category
# ──────────────────────────────────────────────────────────────────────────────


class TestPatronCategoryCli:
    def test_list_shows_seeded(self, session):
        r = _invoke(
            session,
            ["patron-category", "list"],
            "compendium.cli.commands.patron_category",
        )
        assert r.exit_code == 0
        assert "adult" in r.output and "child" in r.output

    def test_create_and_delete(self, session):
        r = _invoke(
            session,
            ["patron-category", "create", "--code", "vipcli", "--name", "VIP"],
            "compendium.cli.commands.patron_category",
        )
        assert r.exit_code == 0
        r = _invoke(
            session,
            ["patron-category", "delete", "--code", "vipcli", "--yes"],
            "compendium.cli.commands.patron_category",
        )
        assert r.exit_code == 0

    def test_cannot_delete_default(self, session):
        r = _invoke(
            session,
            ["patron-category", "delete", "--code", "adult", "--yes"],
            "compendium.cli.commands.patron_category",
        )
        assert r.exit_code == 1


class TestPatronCategoryFlagOnPatron:
    def test_patron_add_with_category_and_expires(self, session):
        r = _invoke(
            session,
            [
                "patron",
                "add",
                "--name",
                "CliCat",
                "--category",
                "child",
                "--expires",
                "2027-12-31",
            ],
            "compendium.cli.commands.patron",
        )
        assert r.exit_code == 0
        assert "child" in r.output
        assert "2027-12-31" in r.output

    def test_patron_add_unknown_category_fails(self, session):
        r = _invoke(
            session,
            ["patron", "add", "--name", "X", "--category", "no-such"],
            "compendium.cli.commands.patron",
        )
        assert r.exit_code != 0


class TestMaintenanceDeactivateExpired:
    def test_no_expired_patrons_message(self, session):
        r = _invoke(
            session,
            ["maintenance", "deactivate-expired-patrons"],
            "compendium.cli.commands.maintenance",
        )
        assert r.exit_code == 0
        assert "No expired" in r.output

    @staticmethod
    def _seed_expired_patron(session, name: str = "Expired Test"):
        from datetime import date, timedelta
        from compendium.repositories.sql.audit_log_repository import (
            SqlAuditLogRepository,
        )
        from compendium.repositories.sql.hold_repository import SqlHoldRepository
        from compendium.repositories.sql.loan_repository import SqlLoanRepository
        from compendium.services.audit import AuditService
        from compendium.services.patrons import PatronService

        svc = PatronService(
            patron_repo=SqlPatronRepository(session),
            loan_repo=SqlLoanRepository(session),
            hold_repo=SqlHoldRepository(session),
            audit_svc=AuditService(SqlAuditLogRepository(session)),
            source="test",
        )
        return svc.create(full_name=name, expires_at=date.today() - timedelta(days=1))

    def test_default_includes_per_patron_detail_line(self, session):
        self._seed_expired_patron(session, name="Detail Patron")
        r = _invoke(
            session,
            ["maintenance", "deactivate-expired-patrons", "--dry-run"],
            "compendium.cli.commands.maintenance",
        )
        assert r.exit_code == 0, r.output
        assert "Would deactivate 1 patron(s):" in r.output
        assert "Detail Patron" in r.output  # per-patron line printed by default

    def test_quiet_suppresses_per_patron_detail_but_keeps_count(self, session):
        self._seed_expired_patron(session, name="Quiet Patron")
        r = _invoke(
            session,
            ["maintenance", "deactivate-expired-patrons", "--dry-run", "--quiet"],
            "compendium.cli.commands.maintenance",
        )
        assert r.exit_code == 0, r.output
        assert "Would deactivate 1 patron(s)." in r.output  # period, not colon
        assert "Quiet Patron" not in r.output  # detail line suppressed


# ──────────────────────────────────────────────────────────────────────────────
# reports
# ──────────────────────────────────────────────────────────────────────────────


class TestReportsCli:
    def test_checkouts_table(self, session):
        r = _invoke(
            session,
            ["reports", "checkouts", "--months", "3"],
            "compendium.cli.commands.reports",
        )
        assert r.exit_code == 0, r.output
        assert "Month" in r.output and "Count" in r.output

    def test_checkouts_csv(self, session):
        r = _invoke(
            session,
            ["reports", "checkouts", "--months", "2", "--format", "csv"],
            "compendium.cli.commands.reports",
        )
        assert r.exit_code == 0, r.output
        assert r.output.splitlines()[0] == "month,count"

    def test_popular_requires_from(self, session):
        r = _invoke(
            session,
            ["reports", "popular"],
            "compendium.cli.commands.reports",
        )
        assert r.exit_code != 0

    def test_popular_with_window(self, session):
        r = _invoke(
            session,
            ["reports", "popular", "--from", "2026-01-01", "--limit", "5"],
            "compendium.cli.commands.reports",
        )
        assert r.exit_code == 0, r.output

    def test_dormant(self, session):
        r = _invoke(
            session,
            ["reports", "dormant", "--not-since", "2025-01-01"],
            "compendium.cli.commands.reports",
        )
        assert r.exit_code == 0, r.output

    def test_overdues(self, session):
        r = _invoke(
            session,
            ["reports", "overdues"],
            "compendium.cli.commands.reports",
        )
        assert r.exit_code == 0, r.output


# ──────────────────────────────────────────────────────────────────────────────
# hold suspend/resume
# ──────────────────────────────────────────────────────────────────────────────


class TestHoldSuspendCli:
    def _seed_waiting_hold(self, session):
        # Seed a work with 1 copy, check it out, place a hold (which will WAIT).
        from compendium.domain.models import Patron as _Patron
        from compendium.repositories.sql.branch_repository import SqlBranchRepository
        from compendium.repositories.sql.hold_repository import SqlHoldRepository
        from compendium.repositories.sql.item_repository import SqlItemRepository
        from compendium.repositories.sql.loan_policy_repository import (
            SqlLoanPolicyRepository,
        )
        from compendium.repositories.sql.loan_repository import SqlLoanRepository
        from compendium.repositories.sql.patron_repository import SqlPatronRepository
        from compendium.repositories.sql.work_repository import SqlWorkRepository
        from compendium.services.circulation import CirculationService
        from compendium.services.holds import HoldService

        work, item = _seed_work(session)
        holder = _Patron(library_card_number="HSHOLD01", full_name="Holder")
        session.add(holder)
        session.flush()
        CirculationService(
            item_repo=SqlItemRepository(session),
            loan_repo=SqlLoanRepository(session),
            patron_repo=SqlPatronRepository(session),
            branch_repo=SqlBranchRepository(session),
            hold_repo=SqlHoldRepository(session),
            policy_repo=SqlLoanPolicyRepository(session),
        ).checkout(item.barcode, "HSHOLD01")
        waiter = _Patron(library_card_number="HSWAIT01", full_name="Waiter")
        session.add(waiter)
        session.flush()
        hold = HoldService(
            hold_repo=SqlHoldRepository(session),
            patron_repo=SqlPatronRepository(session),
            work_repo=SqlWorkRepository(session),
            branch_repo=SqlBranchRepository(session),
            item_repo=SqlItemRepository(session),
        ).place(work.id, "HSWAIT01")
        return hold

    def test_suspend_and_resume_cycle(self, session):
        hold = self._seed_waiting_hold(session)
        # Suspend
        from datetime import date as _date, timedelta as _td
        future = (_date.today() + _td(days=7)).isoformat()
        r = _invoke(
            session,
            ["hold", "suspend", "--id", str(hold.id), "--until", future],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 0, r.output
        assert future in r.output
        # Resume
        r = _invoke(
            session,
            ["hold", "resume", "--id", str(hold.id)],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 0, r.output
        assert "resumed" in r.output.lower()

    def test_suspend_invalid_date_fails(self, session):
        hold = self._seed_waiting_hold(session)
        r = _invoke(
            session,
            ["hold", "suspend", "--id", str(hold.id), "--until", "not-a-date"],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code != 0

    def test_list_suspended_empty(self, session):
        r = _invoke(
            session,
            ["hold", "list-suspended"],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 0
        assert "No suspended holds" in r.output

    def test_maintenance_resume_expired_no_matches(self, session):
        r = _invoke(
            session,
            ["maintenance", "resume-expired-suspends"],
            "compendium.cli.commands.maintenance",
        )
        assert r.exit_code == 0
        assert "No suspended holds" in r.output


class TestLoanVisibilityCli:
    def _seed_loan(self, session):
        from compendium.domain.models import Patron as _Patron
        from compendium.repositories.sql.branch_repository import SqlBranchRepository
        from compendium.repositories.sql.hold_repository import SqlHoldRepository
        from compendium.repositories.sql.item_repository import SqlItemRepository
        from compendium.repositories.sql.loan_policy_repository import (
            SqlLoanPolicyRepository,
        )
        from compendium.repositories.sql.loan_repository import SqlLoanRepository
        from compendium.repositories.sql.patron_repository import SqlPatronRepository
        from compendium.services.circulation import CirculationService

        work, item = _seed_work(session)
        patron = _Patron(library_card_number="LVLIST", full_name="LoanViewer")
        session.add(patron)
        session.flush()
        loan = CirculationService(
            item_repo=SqlItemRepository(session),
            loan_repo=SqlLoanRepository(session),
            patron_repo=SqlPatronRepository(session),
            branch_repo=SqlBranchRepository(session),
            hold_repo=SqlHoldRepository(session),
            policy_repo=SqlLoanPolicyRepository(session),
        ).checkout(item.barcode, "LVLIST")
        return work, item, patron, loan

    def test_loan_list_system_wide(self, session):
        _, item, _, loan = self._seed_loan(session)
        # Table cells can be truncated to fit the (narrow, non-tty) console
        # width, so verify exact values via --format json instead of scraping
        # rendered table text.
        r = _invoke(session, ["loan", "list", "--format", "json"], "compendium.cli.commands.loan")
        assert r.exit_code == 0, r.output
        data = json.loads(r.stdout)
        assert any(row["id"] == loan.id and row["item_barcode"] == item.barcode for row in data)

    def test_loan_history_for_patron(self, session):
        _, item, patron, _ = self._seed_loan(session)
        r = _invoke(
            session,
            ["loan", "history", "--card", patron.library_card_number, "--status", "all", "--format", "json"],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.stdout)
        assert any(row["item_barcode"] == item.barcode for row in data)

    def test_loan_item_history(self, session):
        _, item, patron, _ = self._seed_loan(session)
        r = _invoke(
            session,
            ["loan", "item-history", "--barcode", item.barcode],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 0, r.output
        assert patron.library_card_number in r.output


class TestHoldListVisibilityCli:
    def _seed_work_waiting_hold(self, session):
        from compendium.domain.models import Patron as _Patron
        from compendium.repositories.sql.branch_repository import SqlBranchRepository
        from compendium.repositories.sql.hold_repository import SqlHoldRepository
        from compendium.repositories.sql.item_repository import SqlItemRepository
        from compendium.repositories.sql.loan_policy_repository import (
            SqlLoanPolicyRepository,
        )
        from compendium.repositories.sql.loan_repository import SqlLoanRepository
        from compendium.repositories.sql.patron_repository import SqlPatronRepository
        from compendium.repositories.sql.work_repository import SqlWorkRepository
        from compendium.services.circulation import CirculationService
        from compendium.services.holds import HoldService

        work, item = _seed_work(session)
        holder = _Patron(library_card_number="HVHOLD", full_name="Holder")
        session.add(holder)
        session.flush()
        CirculationService(
            item_repo=SqlItemRepository(session),
            loan_repo=SqlLoanRepository(session),
            patron_repo=SqlPatronRepository(session),
            branch_repo=SqlBranchRepository(session),
            hold_repo=SqlHoldRepository(session),
            policy_repo=SqlLoanPolicyRepository(session),
        ).checkout(item.barcode, "HVHOLD")
        waiter = _Patron(library_card_number="HVWAIT", full_name="Waiter Name")
        session.add(waiter)
        session.flush()
        hold = HoldService(
            hold_repo=SqlHoldRepository(session),
            patron_repo=SqlPatronRepository(session),
            work_repo=SqlWorkRepository(session),
            branch_repo=SqlBranchRepository(session),
            item_repo=SqlItemRepository(session),
        ).place(work.id, "HVWAIT")
        return work, hold

    def test_list_without_card_shows_system_wide(self, session):
        _, hold = self._seed_work_waiting_hold(session)
        r = _invoke(session, ["hold", "list"], "compendium.cli.commands.hold")
        assert r.exit_code == 0, r.output
        # Patron card / status survive the console-width table truncation
        # (the work title column does not) — assert on those instead of the
        # dropped "active hold(s)" count header or a "#<id>" prefix.
        assert "HVWAIT" in r.output
        assert "waiting" in r.output

    def test_list_with_query_filter(self, session):
        self._seed_work_waiting_hold(session)
        r = _invoke(
            session,
            ["hold", "list", "--query", "Waiter"],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 0, r.output
        assert "HVWAIT" in r.output

    def test_queue_command(self, session):
        work, hold = self._seed_work_waiting_hold(session)
        r = _invoke(
            session,
            ["hold", "queue", "--work-id", str(work.id)],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 0, r.output
        assert "HVWAIT" in r.output

    def test_queue_empty(self, session):
        work, _ = _seed_work(session)
        r = _invoke(
            session,
            ["hold", "queue", "--work-id", str(work.id)],
            "compendium.cli.commands.hold",
        )
        assert r.exit_code == 0
        assert "No active holds" in r.output


# ──────────────────────────────────────────────────────────────────────────────
# claims-returned
# ──────────────────────────────────────────────────────────────────────────────


class TestClaimsReturnedCli:
    def test_full_claim_flow(self, session):
        # Seed work + patron + loan
        work, item = _seed_work(session)
        from compendium.domain.models import Patron as _Patron
        p = _Patron(library_card_number="CLICLA01", full_name="Alice")
        session.add(p)
        session.flush()
        # Checkout (CLI)
        r = _invoke(
            session,
            ["loan", "checkout", "--barcode", item.barcode, "--card", p.library_card_number],
            "compendium.cli.commands.loan",
        )
        assert r.exit_code == 0, r.output
        # Claim returned
        r = _invoke(
            session,
            ["claim", "returned", "--barcode", item.barcode, "--note", "last tuesday"],
            "compendium.cli.commands.claim",
        )
        assert r.exit_code == 0, r.output
        assert "claims-returned" in r.output
        # List claims
        r = _invoke(
            session,
            ["claim", "list"],
            "compendium.cli.commands.claim",
        )
        assert r.exit_code == 0, r.output
        assert item.barcode in r.output
        # Verify returned
        r = _invoke(
            session,
            ["claim", "verify", "--barcode", item.barcode],
            "compendium.cli.commands.claim",
        )
        assert r.exit_code == 0, r.output

    def test_write_off_requires_note(self, session):
        r = _invoke(
            session,
            ["claim", "write-off", "--barcode", "NOSUCH"],
            "compendium.cli.commands.claim",
        )
        assert r.exit_code != 0  # typer rejects missing required --note

    def test_verify_rejects_non_claims_item(self, session):
        _, item = _seed_work(session)
        r = _invoke(
            session,
            ["claim", "verify", "--barcode", item.barcode],
            "compendium.cli.commands.claim",
        )
        assert r.exit_code == 1

    def test_empty_claims_list(self, session):
        r = _invoke(
            session,
            ["claim", "list"],
            "compendium.cli.commands.claim",
        )
        assert r.exit_code == 0
        assert "No active claims-returned" in r.output


# ──────────────────────────────────────────────────────────────────────────────
# labels
# ──────────────────────────────────────────────────────────────────────────────


class TestLabelsCli:
    def test_templates_lists_all(self, session):
        r = _invoke(
            session,
            ["labels", "templates"],
            "compendium.cli.commands.labels",
        )
        assert r.exit_code == 0
        for key in ("avery-5160", "avery-5167", "avery-5871", "avery-22806"):
            assert key in r.output

    def test_pocket_produces_pdf(self, session, tmp_path):
        _seed_work(session)
        out = tmp_path / "items.pdf"
        r = _invoke(
            session,
            ["labels", "pocket", "--output", str(out), "--template", "avery-5160"],
            "compendium.cli.commands.labels",
        )
        assert r.exit_code == 0, r.output
        assert out.exists()
        assert out.read_bytes().startswith(b"%PDF-")

    def test_spine_produces_pdf(self, session, tmp_path):
        _seed_work(session)
        out = tmp_path / "spine.pdf"
        r = _invoke(
            session,
            ["labels", "spine", "--output", str(out)],
            "compendium.cli.commands.labels",
        )
        assert r.exit_code == 0, r.output
        assert out.read_bytes().startswith(b"%PDF-")

    def test_barcode_produces_pdf(self, session, tmp_path):
        _seed_work(session)
        out = tmp_path / "barcodes.pdf"
        r = _invoke(
            session,
            ["labels", "barcode", "--output", str(out)],
            "compendium.cli.commands.labels",
        )
        assert r.exit_code == 0, r.output
        assert out.read_bytes().startswith(b"%PDF-")

    def test_pocket_show_branch(self, session, tmp_path):
        _seed_work(session)
        out = tmp_path / "branch.pdf"
        r = _invoke(
            session,
            ["labels", "pocket", "--output", str(out), "--show", "branch"],
            "compendium.cli.commands.labels",
        )
        assert r.exit_code == 0, r.output
        assert out.read_bytes().startswith(b"%PDF-")

    def test_pocket_unknown_template_fails(self, session, tmp_path):
        out = tmp_path / "items.pdf"
        r = _invoke(
            session,
            ["labels", "pocket", "--output", str(out), "--template", "nope"],
            "compendium.cli.commands.labels",
        )
        assert r.exit_code == 1
        assert not out.exists()

    def test_pocket_no_match_fails(self, session, tmp_path):
        out = tmp_path / "items.pdf"
        r = _invoke(
            session,
            [
                "labels", "pocket", "--output", str(out),
                "--barcodes", "DOESNOTEXIST",
            ],
            "compendium.cli.commands.labels",
        )
        assert r.exit_code == 1

    def test_patron_card_produces_pdf(self, session, tmp_path):
        from compendium.domain.models import Patron as _Patron
        p = _Patron(library_card_number="LABCLI01", full_name="CLI Label")
        session.add(p)
        session.flush()
        out = tmp_path / "patrons.pdf"
        r = _invoke(
            session,
            ["labels", "patron-card", "--output", str(out), "--template", "avery-5871"],
            "compendium.cli.commands.labels",
        )
        assert r.exit_code == 0, r.output
        assert out.read_bytes().startswith(b"%PDF-")

    def test_patron_sticker_produces_pdf(self, session, tmp_path):
        from compendium.domain.models import Patron as _Patron
        p = _Patron(library_card_number="LABCLI02", full_name="Sticker")
        session.add(p)
        session.flush()
        out = tmp_path / "stickers.pdf"
        r = _invoke(
            session,
            ["labels", "patron-sticker", "--output", str(out), "--template", "avery-5167"],
            "compendium.cli.commands.labels",
        )
        assert r.exit_code == 0, r.output
        assert out.read_bytes().startswith(b"%PDF-")


# ──────────────────────────────────────────────────────────────────────────────
# db init — exercises migration + seed path on a fresh SQLite file
# ──────────────────────────────────────────────────────────────────────────────


class TestDbInitCli:
    def test_db_init_on_fresh_file(self, tmp_path, monkeypatch):
        # Point at a fresh file DB so Alembic's upgrade runs end-to-end.
        db_file = tmp_path / "cli_init.db"
        monkeypatch.setenv("COMPENDIUM_DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.setenv("COMPENDIUM_JWT_SECRET_KEY", "a" * 48)
        from compendium.db import engine as _engine
        _engine.get_settings.cache_clear()
        _engine._server_engine.cache_clear()
        try:
            r = CliRunner().invoke(app, ["db", "init"])
            assert r.exit_code == 0, r.output
            assert db_file.exists()
            # Re-running should be idempotent.
            r = CliRunner().invoke(app, ["db", "init"])
            assert r.exit_code == 0
        finally:
            _engine.get_settings.cache_clear()
            _engine._server_engine.cache_clear()

    def test_db_upgrade(self, tmp_path, monkeypatch):
        db_file = tmp_path / "cli_upgrade.db"
        monkeypatch.setenv("COMPENDIUM_DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.setenv("COMPENDIUM_JWT_SECRET_KEY", "a" * 48)
        from compendium.db import engine as _engine
        _engine.get_settings.cache_clear()
        _engine._server_engine.cache_clear()
        try:
            r = CliRunner().invoke(app, ["db", "upgrade"])
            assert r.exit_code == 0
            assert "Migrations applied" in r.output
        finally:
            _engine.get_settings.cache_clear()
            _engine._server_engine.cache_clear()


# ──────────────────────────────────────────────────────────────────────────────
# backup / restore — end-to-end CLI roundtrip against fresh SQLite files
# ──────────────────────────────────────────────────────────────────────────────


class TestBackupCli:
    def test_backup_and_restore_roundtrip(self, tmp_path, monkeypatch):
        src = tmp_path / "src.db"
        monkeypatch.setenv("COMPENDIUM_DATABASE_URL", f"sqlite:///{src}")
        monkeypatch.setenv("COMPENDIUM_JWT_SECRET_KEY", "a" * 48)
        from compendium.db import engine as _engine
        _engine.get_settings.cache_clear()
        _engine._server_engine.cache_clear()
        try:
            assert CliRunner().invoke(app, ["db", "init"]).exit_code == 0
            assert CliRunner().invoke(
                app, ["user", "add", "--username", "alice",
                      "--password", "secret1234", "--role", "Librarian"]
            ).exit_code == 0

            archive = tmp_path / "backup.tar.gz"
            r = CliRunner().invoke(app, ["backup", "--output", str(archive)])
            assert r.exit_code == 0, r.output
            assert archive.exists()
            assert "rows total" in r.output

            # Restore into a fresh empty DB
            dst = tmp_path / "dst.db"
            monkeypatch.setenv("COMPENDIUM_DATABASE_URL", f"sqlite:///{dst}")
            _engine.get_settings.cache_clear()
            _engine._server_engine.cache_clear()
            r = CliRunner().invoke(
                app, ["restore", str(archive), "--no-covers"]
            )
            assert r.exit_code == 0, r.output
            assert "Restored" in r.output

            # Verify alice landed in the destination
            r = CliRunner().invoke(app, ["user", "list"])
            assert "alice" in r.output
        finally:
            _engine.get_settings.cache_clear()
            _engine._server_engine.cache_clear()

    def test_restore_refuses_populated_db_without_force(self, tmp_path, monkeypatch):
        db = tmp_path / "live.db"
        monkeypatch.setenv("COMPENDIUM_DATABASE_URL", f"sqlite:///{db}")
        monkeypatch.setenv("COMPENDIUM_JWT_SECRET_KEY", "a" * 48)
        from compendium.db import engine as _engine
        _engine.get_settings.cache_clear()
        _engine._server_engine.cache_clear()
        try:
            assert CliRunner().invoke(app, ["db", "init"]).exit_code == 0
            assert CliRunner().invoke(
                app, ["user", "add", "--username", "alice",
                      "--password", "secret1234", "--role", "Librarian"]
            ).exit_code == 0
            archive = tmp_path / "backup.tar.gz"
            assert CliRunner().invoke(
                app, ["backup", "--output", str(archive)]
            ).exit_code == 0

            # Restoring over the existing DB (which has alice) requires --force
            r = CliRunner().invoke(app, ["restore", str(archive), "--no-covers"])
            assert r.exit_code == 1
            assert "--force" in r.output
        finally:
            _engine.get_settings.cache_clear()
            _engine._server_engine.cache_clear()

    def test_backup_no_audit_flag_honored(self, tmp_path, monkeypatch):
        src = tmp_path / "src.db"
        monkeypatch.setenv("COMPENDIUM_DATABASE_URL", f"sqlite:///{src}")
        monkeypatch.setenv("COMPENDIUM_JWT_SECRET_KEY", "a" * 48)
        from compendium.db import engine as _engine
        _engine.get_settings.cache_clear()
        _engine._server_engine.cache_clear()
        try:
            assert CliRunner().invoke(app, ["db", "init"]).exit_code == 0
            archive = tmp_path / "backup.tar.gz"
            r = CliRunner().invoke(
                app, ["backup", "--output", str(archive), "--no-audit"]
            )
            assert r.exit_code == 0, r.output
            import json, tarfile
            with tarfile.open(archive, "r:gz") as tar:
                with tar.extractfile("meta.json") as f:
                    manifest = json.loads(f.read().decode())
            assert manifest["include_audit"] is False
            assert manifest["tables"]["audit_log"] == 0
        finally:
            _engine.get_settings.cache_clear()
            _engine._server_engine.cache_clear()


# ──────────────────────────────────────────────────────────────────────────────
# malformed date usage errors
# ──────────────────────────────────────────────────────────────────────────────


def test_patron_add_bad_expires_is_clean_usage_error(session):
    r = _invoke(
        session,
        ["patron", "add", "--name", "Bad Date", "--expires", "2026-13-40"],
        "compendium.cli.commands.patron",
    )
    assert r.exit_code == 2, r.output
    assert "YYYY-MM-DD" in r.output
    assert "Traceback" not in r.output


def test_patron_set_bad_expires_is_clean_usage_error(session):
    add = _invoke(
        session,
        ["patron", "add", "--name", "Date Setter"],
        "compendium.cli.commands.patron",
    )
    assert add.exit_code == 0, add.output
    card = next(
        line.split(":", 1)[1].strip()
        for line in add.output.splitlines()
        if "Card number" in line
    )
    r = _invoke(
        session,
        ["patron", "set", "--card", card, "--expires", "not-a-date"],
        "compendium.cli.commands.patron",
    )
    assert r.exit_code == 2, r.output
    assert "YYYY-MM-DD" in r.output
    assert "Traceback" not in r.output


def _add_patron_get_card(session, name: str) -> str:
    add = _invoke(
        session,
        ["patron", "add", "--name", name],
        "compendium.cli.commands.patron",
    )
    assert add.exit_code == 0, add.output
    return next(
        line.split(":", 1)[1].strip()
        for line in add.output.splitlines()
        if "Card number" in line
    )


def test_patron_edit_name_persists(session):
    card = _add_patron_get_card(session, "Old Name")
    r = _invoke(
        session,
        ["patron", "edit", card, "--name", "New Name"],
        "compendium.cli.commands.patron",
    )
    assert r.exit_code == 0, r.output
    patron = SqlPatronRepository(session).get_by_card_number(card)
    assert patron.full_name == "New Name"


def test_patron_edit_email_and_phone_persist(session):
    card = _add_patron_get_card(session, "Contact Patron")
    r = _invoke(
        session,
        [
            "patron",
            "edit",
            card,
            "--email",
            "new@example.com",
            "--phone",
            "555-1234",
        ],
        "compendium.cli.commands.patron",
    )
    assert r.exit_code == 0, r.output
    patron = SqlPatronRepository(session).get_by_card_number(card)
    assert patron.contact_email == "new@example.com"
    assert patron.contact_phone == "555-1234"


def test_patron_edit_clear_email(session):
    card = _add_patron_get_card(session, "Clear Email Patron")
    set_r = _invoke(
        session,
        ["patron", "edit", card, "--email", "temp@example.com"],
        "compendium.cli.commands.patron",
    )
    assert set_r.exit_code == 0, set_r.output
    r = _invoke(
        session,
        ["patron", "edit", card, "--clear-email"],
        "compendium.cli.commands.patron",
    )
    assert r.exit_code == 0, r.output
    patron = SqlPatronRepository(session).get_by_card_number(card)
    assert patron.contact_email is None


def test_patron_edit_email_and_clear_email_conflict(session):
    card = _add_patron_get_card(session, "Conflict Patron")
    r = _invoke(
        session,
        ["patron", "edit", card, "--email", "x@example.com", "--clear-email"],
        "compendium.cli.commands.patron",
    )
    assert r.exit_code != 0, r.output
    assert "Traceback" not in r.output


def test_reports_popular_bad_from_is_clean_usage_error(session):
    r = _invoke(
        session,
        ["reports", "popular", "--from", "2026-99-99"],
        "compendium.cli.commands.reports",
    )
    assert r.exit_code == 2, r.output
    assert "--from" in r.output and "YYYY-MM-DD" in r.output
    assert "Traceback" not in r.output


def test_reports_dormant_bad_not_since_is_clean_usage_error(session):
    r = _invoke(
        session,
        ["reports", "dormant", "--not-since", "yesterday"],
        "compendium.cli.commands.reports",
    )
    assert r.exit_code == 2, r.output
    assert "--not-since" in r.output and "YYYY-MM-DD" in r.output
    assert "Traceback" not in r.output


def test_labels_spine_bad_since_is_clean_usage_error(session):
    r = _invoke(
        session,
        ["labels", "spine", "-o", "ignored.pdf", "--since", "not-a-date"],
        "compendium.cli.commands.labels",
    )
    assert r.exit_code == 2, r.output
    assert "--since" in r.output and "YYYY-MM-DD" in r.output
    assert "Traceback" not in r.output
