"""Positional natural keys work; legacy option spellings still work; conflicts exit 2.

Pattern mirrors ``tests/integration/test_cli_commands.py``: patch ``session_scope``
in the command module under test to yield the shared test ``session`` fixture so
the CLI runs against the same SQLite-in-memory DB every other integration test uses.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from typer.testing import CliRunner

from compendium.cli.main import app
from compendium.domain.models import Patron
from compendium.repositories.sql.patron_repository import SqlPatronRepository

runner = CliRunner()


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

    with patch(f"{module_path}.session_scope", _scope):
        return runner.invoke(app, args, input=input, env=env)


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


def _seed_patron(session, card: str = "POSID001", name: str = "Positional Patron") -> Patron:
    patron = Patron(library_card_number=card, full_name=name)
    SqlPatronRepository(session).add(patron)
    session.flush()
    return patron


# ──────────────────────────────────────────────────────────────────────────────
# item edit — positional/option equivalence, conflicts, loanable fold-in
# ──────────────────────────────────────────────────────────────────────────────


def test_item_edit_positional_and_option_equivalent(session):
    _, item = _seed_work(session)
    pos = _invoke(
        session,
        ["item", "edit", item.barcode, "--location", "A1"],
        "compendium.cli.commands.item",
    )
    assert pos.exit_code == 0, pos.output
    opt = _invoke(
        session,
        ["item", "edit", "--barcode", item.barcode, "--location", "A2"],
        "compendium.cli.commands.item",
    )
    assert opt.exit_code == 0, opt.output


def test_item_edit_conflicting_identifiers_exit_2(session):
    _, item = _seed_work(session)
    result = _invoke(
        session,
        ["item", "edit", item.barcode, "--barcode", "OTHER", "--location", "A1"],
        "compendium.cli.commands.item",
    )
    assert result.exit_code == 2


def test_item_edit_missing_identifier_exit_2(session):
    result = _invoke(
        session,
        ["item", "edit", "--location", "A1"],
        "compendium.cli.commands.item",
    )
    assert result.exit_code == 2


def test_item_edit_loanable_flag_replaces_set_loanable(session):
    _, item = _seed_work(session)
    result = _invoke(
        session,
        ["item", "edit", item.barcode, "--no-loanable", "--loan-note", "Reference only"],
        "compendium.cli.commands.item",
    )
    assert result.exit_code == 0, result.output
    assert "no" in result.output.lower()

    old = _invoke(
        session,
        ["item", "set-loanable", "--barcode", item.barcode, "--yes"],
        "compendium.cli.commands.item",
    )
    assert old.exit_code == 0, old.output  # hidden alias still functional

    help_out = _invoke(session, ["item", "--help"], "compendium.cli.commands.item")
    assert "set-loanable" not in help_out.output


# ──────────────────────────────────────────────────────────────────────────────
# item — remaining barcode-identified commands
# ──────────────────────────────────────────────────────────────────────────────


def test_item_withdraw_positional(session):
    _, item = _seed_work(session)
    result = _invoke(
        session,
        ["item", "withdraw", item.barcode],
        "compendium.cli.commands.item",
    )
    assert result.exit_code == 0, result.output
    assert "Withdrawn" in result.output


def test_item_declare_lost_positional(session):
    _, item = _seed_work(session)
    patron = _seed_patron(session, card="POSID002")
    checkout = _invoke(
        session,
        ["loan", "checkout", "--barcode", item.barcode, "--card", patron.library_card_number],
        "compendium.cli.commands.loan",
    )
    assert checkout.exit_code == 0, checkout.output
    result = _invoke(
        session,
        ["item", "declare-lost", item.barcode, "--replacement-cost-cents", "2500"],
        "compendium.cli.commands.item",
    )
    assert result.exit_code == 0, result.output
    assert "declared lost" in result.output


def test_item_clear_lost_positional(session):
    _, item = _seed_work(session)
    patron = _seed_patron(session, card="POSID003")
    _invoke(
        session,
        ["loan", "checkout", "--barcode", item.barcode, "--card", patron.library_card_number],
        "compendium.cli.commands.loan",
    )
    _invoke(
        session,
        ["item", "declare-lost", item.barcode, "--replacement-cost-cents", "2500"],
        "compendium.cli.commands.item",
    )
    result = _invoke(
        session,
        ["item", "clear-lost", item.barcode],
        "compendium.cli.commands.item",
    )
    assert result.exit_code == 0, result.output
    assert "recovered" in result.output


def test_item_mark_damaged_and_clear_damage_positional(session):
    _, item = _seed_work(session)
    patron = _seed_patron(session, card="POSID004")
    _invoke(
        session,
        ["loan", "checkout", "--barcode", item.barcode, "--card", patron.library_card_number],
        "compendium.cli.commands.loan",
    )
    result = _invoke(
        session,
        ["item", "mark-damaged", item.barcode, "--amount-cents", "500", "--note", "cover torn"],
        "compendium.cli.commands.item",
    )
    assert result.exit_code == 0, result.output
    assert "damaged" in result.output
    result = _invoke(
        session,
        ["item", "clear-damage", item.barcode],
        "compendium.cli.commands.item",
    )
    assert result.exit_code == 0, result.output


# ──────────────────────────────────────────────────────────────────────────────
# patron
# ──────────────────────────────────────────────────────────────────────────────


def test_patron_deactivate_positional(session):
    patron = _seed_patron(session, card="POSID005")
    result = _invoke(
        session,
        ["patron", "deactivate", patron.library_card_number],
        "compendium.cli.commands.patron",
    )
    assert result.exit_code == 0, result.output
    assert "Deactivated" in result.output


def test_patron_reactivate_positional(session):
    patron = _seed_patron(session, card="POSID006")
    _invoke(session, ["patron", "deactivate", patron.library_card_number], "compendium.cli.commands.patron")
    result = _invoke(
        session,
        ["patron", "reactivate", patron.library_card_number],
        "compendium.cli.commands.patron",
    )
    assert result.exit_code == 0, result.output
    assert "Reactivated" in result.output


def test_patron_edit_positional_and_option_equivalent(session):
    patron = _seed_patron(session, card="POSID007")
    pos = _invoke(
        session,
        ["patron", "edit", patron.library_card_number, "--expires", "2030-01-01"],
        "compendium.cli.commands.patron",
    )
    assert pos.exit_code == 0, pos.output
    opt = _invoke(
        session,
        ["patron", "edit", "--card", patron.library_card_number, "--clear-expires"],
        "compendium.cli.commands.patron",
    )
    assert opt.exit_code == 0, opt.output


def test_patron_link_unlink_user_positional(session):
    _invoke(
        session,
        ["user", "add", "--username", "posidlink", "--password", "s", "--role", "Patron"],
        "compendium.cli.commands.user",
    )
    patron = _seed_patron(session, card="POSID008")
    result = _invoke(
        session,
        ["patron", "link-user", patron.library_card_number, "--username", "posidlink"],
        "compendium.cli.commands.patron",
    )
    assert result.exit_code == 0, result.output
    assert "Linked" in result.output
    result = _invoke(
        session,
        ["patron", "unlink-user", patron.library_card_number],
        "compendium.cli.commands.patron",
    )
    assert result.exit_code == 0, result.output
    assert "Unlinked" in result.output


def test_patron_add_user_positional(session):
    patron = _seed_patron(session, card="POSID009")
    result = _invoke(
        session,
        ["patron", "add-user", patron.library_card_number, "--username", "posidadduser", "--password", "s3cret123"],
        "compendium.cli.commands.patron",
    )
    assert result.exit_code == 0, result.output
    assert "Created login" in result.output


def test_patron_conflicting_identifiers_exit_2(session):
    patron = _seed_patron(session, card="POSID010")
    result = _invoke(
        session,
        ["patron", "deactivate", patron.library_card_number, "--card", "OTHER"],
        "compendium.cli.commands.patron",
    )
    assert result.exit_code == 2


# ──────────────────────────────────────────────────────────────────────────────
# user
# ──────────────────────────────────────────────────────────────────────────────


def test_user_deactivate_reactivate_positional(session):
    _invoke(
        session,
        ["user", "add", "--username", "posiduser", "--password", "s", "--role", "Patron"],
        "compendium.cli.commands.user",
    )
    result = _invoke(
        session,
        ["user", "deactivate", "posiduser"],
        "compendium.cli.commands.user",
    )
    assert result.exit_code == 0, result.output
    result = _invoke(
        session,
        ["user", "reactivate", "posiduser"],
        "compendium.cli.commands.user",
    )
    assert result.exit_code == 0, result.output


def test_user_link_unlink_patron_positional(session):
    _invoke(
        session,
        ["user", "add", "--username", "posiduser2", "--password", "s", "--role", "Patron"],
        "compendium.cli.commands.user",
    )
    patron = _seed_patron(session, card="POSID011")
    result = _invoke(
        session,
        ["user", "link-patron", "posiduser2", "--card", patron.library_card_number],
        "compendium.cli.commands.user",
    )
    assert result.exit_code == 0, result.output
    result = _invoke(
        session,
        ["user", "unlink-patron", "posiduser2"],
        "compendium.cli.commands.user",
    )
    assert result.exit_code == 0, result.output


def test_user_conflicting_identifiers_exit_2(session):
    _invoke(
        session,
        ["user", "add", "--username", "posiduser3", "--password", "s", "--role", "Patron"],
        "compendium.cli.commands.user",
    )
    result = _invoke(
        session,
        ["user", "deactivate", "posiduser3", "--username", "other"],
        "compendium.cli.commands.user",
    )
    assert result.exit_code == 2


# ──────────────────────────────────────────────────────────────────────────────
# branch
# ──────────────────────────────────────────────────────────────────────────────


def test_branch_edit_positional(session):
    r = _invoke(session, ["branch", "list", "--format", "json"], "compendium.cli.commands.branch")
    import json

    code = next(row["code"] for row in json.loads(r.stdout) if row["is_default"])
    result = _invoke(
        session,
        ["branch", "edit", code, "--classification", "ddc"],
        "compendium.cli.commands.branch",
    )
    assert result.exit_code == 0, result.output
    assert "DDC" in result.output


def test_branch_edit_conflicting_identifiers_exit_2(session):
    result = _invoke(
        session,
        ["branch", "edit", "MAIN", "--code", "OTHER", "--classification", "lcc"],
        "compendium.cli.commands.branch",
    )
    assert result.exit_code == 2


# ──────────────────────────────────────────────────────────────────────────────
# patron-category
# ──────────────────────────────────────────────────────────────────────────────


def test_patron_category_edit_and_delete_positional(session):
    create = _invoke(
        session,
        ["patron-category", "add", "--code", "posidcat", "--name", "Positional Cat"],
        "compendium.cli.commands.patron_category",
    )
    assert create.exit_code == 0, create.output
    edit = _invoke(
        session,
        ["patron-category", "edit", "posidcat", "--name", "Renamed Cat"],
        "compendium.cli.commands.patron_category",
    )
    assert edit.exit_code == 0, edit.output
    delete = _invoke(
        session,
        ["patron-category", "delete", "posidcat"],
        "compendium.cli.commands.patron_category",
    )
    assert delete.exit_code == 0, delete.output


def test_patron_category_conflicting_identifiers_exit_2(session):
    _invoke(
        session,
        ["patron-category", "add", "--code", "posidcat2", "--name", "Positional Cat 2"],
        "compendium.cli.commands.patron_category",
    )
    result = _invoke(
        session,
        ["patron-category", "edit", "posidcat2", "--code", "other", "--name", "X"],
        "compendium.cli.commands.patron_category",
    )
    assert result.exit_code == 2
