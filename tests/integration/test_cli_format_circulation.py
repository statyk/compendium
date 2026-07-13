"""--format coverage for the circulation group: loan, hold, claim, fine.

Pattern mirrors ``tests/integration/test_trash_cli.py`` / ``test_cli_reports_format.py``:
patch ``session_scope`` in the command module under test to yield the shared
``session`` fixture, so the CLI runs against the same in-memory DB the rest
of the integration suite uses.
"""

from __future__ import annotations

import itertools
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from typer.testing import CliRunner

from compendium.cli.main import app
from compendium.domain.models import (
    Branch,
    Creator,
    Fine,
    Item,
    Loan,
    MediaType,
    Patron,
    Work,
    WorkCreator,
)

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


def _mk_work(session, *, title="Dune", n_items=1):
    seq = next(_mk_seq)
    branch = session.query(Branch).first()
    media = session.query(MediaType).filter_by(code="book").first()
    creator = Creator(display_name="Frank Herbert", sort_name="Herbert, Frank")
    work = Work(
        title=title,
        media_type_id=media.id,
        isbn=f"978000000{seq:04d}",
        search_text=title,
    )
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


def _mk_patron(session, *, name="Format Patron"):
    seq = next(_mk_seq)
    patron = Patron(library_card_number=f"FMT-{seq}", full_name=name)
    session.add(patron)
    session.flush()
    return patron


def seeded_loan(session):
    branch = session.query(Branch).first()
    work, items = _mk_work(session, title="Loan Fixture Work")
    patron = _mk_patron(session, name="Loan Fixture Patron")
    loan = Loan(
        item_id=items[0].id,
        patron_id=patron.id,
        branch_id=branch.id,
        due_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    session.add(loan)
    session.flush()
    return loan


def seeded_fine(session):
    patron = _mk_patron(session, name="Fine Fixture Patron")
    fine = Fine(
        patron_id=patron.id,
        kind="other",
        amount_cents=500,
        status="outstanding",
    )
    session.add(fine)
    session.flush()
    return fine


def test_loan_list_rejects_bad_format(session):
    res = _invoke(session, "loan", ["loan", "list", "--format", "yaml"])
    assert res.exit_code == 2


def test_loan_list_json(session):
    seeded_loan(session)
    res = _invoke(session, "loan", ["loan", "list", "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert {
        "id", "patron_card", "item_barcode", "title", "due_at",
        "checked_out_at", "returned_at", "renewal_count", "overdue",
    } <= set(data[0])


def test_loan_list_table(session):
    seeded_loan(session)
    res = _invoke(session, "loan", ["loan", "list"])
    assert res.exit_code == 0 and "Due" in res.stdout


def test_hold_list_json_empty(session):
    res = _invoke(session, "hold", ["hold", "list", "--format", "json"])
    assert res.exit_code == 0 and json.loads(res.stdout) == []


def test_fine_list_json(session):
    seeded_fine(session)
    res = _invoke(session, "fine", ["fine", "list", "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data[0]["amount_cents"] == 500 and data[0]["status"] == "outstanding"


def test_claim_list_table_empty(session):
    res = _invoke(session, "claim", ["claim", "list"])
    assert res.exit_code == 0 and "No active claims-returned" in res.stdout
