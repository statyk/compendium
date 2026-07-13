"""--format coverage for the catalog group: item, work.

Pattern mirrors ``tests/integration/test_trash_cli.py`` /
``test_cli_format_circulation.py``: patch ``session_scope`` in the command
module under test to yield the shared ``session`` fixture, so the CLI runs
against the same in-memory DB the rest of the integration suite uses.
"""

from __future__ import annotations

import itertools
import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from compendium.cli.main import app
from compendium.domain.models import Branch, Creator, Item, MediaType, Work, WorkCreator

_mk_seq = itertools.count(1)


def _invoke(session, module: str, args: list[str]):
    @contextmanager
    def _scope():
        yield session

    runner = CliRunner()
    # NOTE: the installed click (8.3.x) removed CliRunner's mix_stderr
    # kwarg — stdout/stderr are separate streams by default now, which is
    # exactly what we want (result.stdout / result.stderr below).
    with patch(f"compendium.cli.commands.{module}.session_scope", _scope):
        return runner.invoke(app, args)


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


@pytest.fixture
def seeded_work(session):
    work, items = _mk_work(session, title="Dune", n_items=2)
    work.items = items
    return work


def test_item_list_json(session, seeded_work):
    res = _invoke(session, "item", ["item", "list", "--format", "json"])
    data = json.loads(res.stdout)
    assert {"id", "title", "media_type", "creators", "publication_year",
            "copies"} <= set(data[0])


def test_item_show_json(session, seeded_work):
    barcode = seeded_work.items[0].barcode
    res = _invoke(session, "item", ["item", "show", barcode, "--format", "json"])
    data = json.loads(res.stdout)
    assert data["barcode"] == barcode and "status" in data


def test_work_search_json(session, seeded_work):
    res = _invoke(session, "work", ["work", "search", "Dune", "--format", "json"])
    assert json.loads(res.stdout)[0]["title"] == "Dune"


def test_work_show_table(session, seeded_work):
    res = _invoke(session, "work", ["work", "show", str(seeded_work.id)])
    assert res.exit_code == 0 and "Copies" in res.stdout


def test_trash_list_json_empty(session):
    res = _invoke(session, "work", ["work", "trash", "list", "--format", "json"])
    assert json.loads(res.stdout) == []
