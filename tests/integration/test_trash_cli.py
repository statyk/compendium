"""CLI smoke tests for `work delete` / `work trash list|restore|purge`.

Pattern mirrors ``tests/integration/test_cli_commands.py``: patch
``session_scope`` in the command module under test to yield the shared
``session`` fixture, so the CLI runs against the same in-memory DB the rest
of the integration suite uses.
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from typer.testing import CliRunner

from compendium.cli.main import app
from compendium.domain.models import (
    Branch,
    Creator,
    DeletedEntity,
    Item,
    Loan,
    MediaType,
    Patron,
    Work,
    WorkCreator,
)

_mk_seq = itertools.count(1)


def _invoke(session, args: list[str], input: str | None = None):
    @contextmanager
    def _scope():
        yield session

    runner = CliRunner()
    with patch("compendium.cli.commands.work.session_scope", _scope):
        return runner.invoke(app, args, input=input)


def _mk_work(session, *, title="Dune", n_items=2):
    seq = next(_mk_seq)
    branch = session.query(Branch).first()
    media = session.query(MediaType).filter_by(code="book").first()
    creator = Creator(display_name="Frank Herbert", sort_name="Herbert, Frank")
    work = Work(title=title, media_type_id=media.id, isbn=f"978000000{seq:04d}", search_text=title)
    work.creators.append(WorkCreator(creator=creator, role="author", display_order=0))
    session.add(work)
    session.flush()
    items = []
    for i in range(n_items):
        item = Item(
            work_id=work.id, branch_id=branch.id,
            barcode=f"BC-{seq}-{i}", accession_number=f"ACC-{seq}-{i}",
        )
        session.add(item)
        items.append(item)
    session.flush()
    return work, items


def test_work_delete_and_trash_list_restore(session):
    work, items = _mk_work(session, title="Dune", n_items=1)
    work_id = work.id

    result = _invoke(session, ["work", "delete", str(work_id), "--yes"])
    assert result.exit_code == 0, result.output
    assert "Moved to trash" in result.output

    # Live work is gone.
    assert session.get(Work, work_id) is None

    result = _invoke(session, ["work", "trash", "list"])
    assert result.exit_code == 0, result.output
    assert "1 copy" in result.output or "copies" in result.output

    trash_row = (
        session.query(DeletedEntity).order_by(DeletedEntity.id.desc()).first()
    )
    trash_id = trash_row.id

    result = _invoke(session, ["work", "trash", "restore", str(trash_id)])
    assert result.exit_code == 0, result.output
    assert "Restored" in result.output

    # Trash row consumed, work is back in the live catalog.
    assert session.get(DeletedEntity, trash_id) is None
    restored = session.query(Work).filter_by(title="Dune").one()
    assert restored.items and len(restored.items) == 1


def test_work_delete_blocked_exits_nonzero(session):
    work, items = _mk_work(session, title="Loaned Out", n_items=1)
    branch = session.query(Branch).first()
    patron = Patron(library_card_number=f"CARD-{next(_mk_seq)}", full_name="Pat Ron")
    session.add(patron)
    session.flush()
    session.add(Loan(
        item_id=items[0].id, patron_id=patron.id, branch_id=branch.id,
        due_at=datetime.now(timezone.utc) + timedelta(days=7),
    ))
    session.flush()

    result = _invoke(session, ["work", "delete", str(work.id), "--yes"])
    assert result.exit_code == 1
    assert "active loan" in result.output

    # Live work untouched.
    assert session.get(Work, work.id) is not None


def test_work_delete_missing_work_exits_nonzero(session):
    result = _invoke(session, ["work", "delete", "999999", "--yes"])
    assert result.exit_code == 1
    assert "no work with id" in result.output.lower()


def test_work_delete_without_yes_prompts_and_shows_copy_count(session):
    work, items = _mk_work(session, title="Confirm Me", n_items=2)

    result = _invoke(session, ["work", "delete", str(work.id)], input="y\n")
    assert result.exit_code == 0, result.output
    assert "2 copies" in result.output
    assert "Moved to trash" in result.output


def test_work_delete_without_yes_aborts_on_no(session):
    work, items = _mk_work(session, title="Keep Me", n_items=1)

    result = _invoke(session, ["work", "delete", str(work.id)], input="n\n")
    assert result.exit_code != 0
    assert session.get(Work, work.id) is not None


def test_work_trash_restore_missing_id_exits_nonzero(session):
    result = _invoke(session, ["work", "trash", "restore", "999999"])
    assert result.exit_code == 1
    assert "no deleted work" in result.output.lower()


def test_work_trash_list_empty(session):
    result = _invoke(session, ["work", "trash", "list"])
    assert result.exit_code == 0
    assert "Trash is empty." in result.output


def test_work_trash_purge_requires_arg(session):
    result = _invoke(session, ["work", "trash", "purge"])
    assert result.exit_code != 0


def test_work_trash_purge_by_id(session):
    work, items = _mk_work(session, title="Purge By Id", n_items=1)
    _invoke(session, ["work", "delete", str(work.id), "--yes"])
    trash_row = session.query(DeletedEntity).order_by(DeletedEntity.id.desc()).first()

    result = _invoke(session, ["work", "trash", "purge", str(trash_row.id)])
    assert result.exit_code == 0, result.output
    assert "Purged 1 trash entry." in result.output
    assert session.get(DeletedEntity, trash_row.id) is None


def test_work_trash_purge_by_older_than_days(session):
    work, items = _mk_work(session, title="Purge By Age", n_items=1)
    _invoke(session, ["work", "delete", str(work.id), "--yes"])
    trash_row = session.query(DeletedEntity).order_by(DeletedEntity.id.desc()).first()
    trash_row.deleted_at = datetime.now(timezone.utc) - timedelta(days=200)
    session.flush()

    result = _invoke(session, ["work", "trash", "purge", "--older-than-days", "90"])
    assert result.exit_code == 0, result.output
    assert "Purged 1 trash entry." in result.output
    assert session.get(DeletedEntity, trash_row.id) is None


def test_work_trash_purge_both_args_is_domain_error(session):
    result = _invoke(session, ["work", "trash", "purge", "1", "--older-than-days", "5"])
    assert result.exit_code != 0
