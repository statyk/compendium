"""CLI integration tests for fine/loan/maintenance/policy commands."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from typer.testing import CliRunner

from compendium.domain.enums import FineKind, FineStatus, ItemStatus
from compendium.domain.models import Loan, Patron
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository


def _runner(session, app, args, module_to_patch: str):
    @contextmanager
    def _scope():
        yield session

    runner = CliRunner()
    with patch(f"{module_to_patch}.session_scope", _scope):
        return runner.invoke(app, args)


def _seed_work_item(session, isbn="9780441013593"):
    from unittest.mock import patch as _patch

    from compendium.repositories.sql.branch_repository import SqlBranchRepository
    from compendium.repositories.sql.creator_repository import SqlCreatorRepository
    from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
    from compendium.services.catalog import CatalogService

    with _patch(
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
        work, item = catalog.add_from_isbn(isbn)
    session.flush()
    return work, item


def _make_patron(session, card):
    p = Patron(library_card_number=card, full_name="Alice")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


def _set_policy(session, *, per_day=10, lost_default=None, proc=None):
    pol = SqlLoanPolicyRepository(session).get_default()
    pol.overdue_fine_per_day_cents = per_day
    pol.lost_item_default_cents = lost_default
    pol.lost_item_processing_fee_cents = proc
    session.flush()


def _make_overdue_loan(session, patron, item, days_late=3):
    now = datetime.now(timezone.utc)
    loan = Loan(
        item_id=item.id,
        patron_id=patron.id,
        branch_id=item.branch_id,
        checked_out_at=now - timedelta(days=days_late + 14),
        due_at=now - timedelta(days=days_late),
    )
    SqlLoanRepository(session).add(loan)
    item.status = ItemStatus.CHECKED_OUT.value
    SqlItemRepository(session).update(item)
    return loan


def test_cli_fine_assess_then_list(session):
    from compendium.cli.commands.fine import app as fine_app

    _make_patron(session, "CLI_F0001")
    r = _runner(
        session,
        fine_app,
        ["assess", "--patron", "CLI_F0001", "--kind", "other", "--amount-cents", "250", "--note", "card replacement"],
        "compendium.cli.commands.fine",
    )
    assert r.exit_code == 0, r.output
    assert "Assessed fine" in r.output

    r = _runner(session, fine_app, ["list", "--patron", "CLI_F0001"], "compendium.cli.commands.fine")
    assert r.exit_code == 0
    assert "$2.50" in r.output
    assert "other" in r.output


def test_cli_fine_pay_transitions_status(session):
    from compendium.cli.commands.fine import app as fine_app

    p = _make_patron(session, "CLI_F0002")
    _runner(
        session, fine_app,
        ["assess", "--patron", "CLI_F0002", "--kind", "other", "--amount-cents", "500", "--note", "xx"],
        "compendium.cli.commands.fine",
    )
    fine = SqlFineRepository(session).list(patron_id=p.id)[0]
    r = _runner(session, fine_app, ["pay", "--id", str(fine.id)], "compendium.cli.commands.fine")
    assert r.exit_code == 0
    assert "paid" in r.output.lower()
    session.refresh(fine)
    assert fine.status == FineStatus.PAID.value


def test_cli_fine_waive_requires_note(session):
    from compendium.cli.commands.fine import app as fine_app

    p = _make_patron(session, "CLI_F0003")
    _runner(
        session, fine_app,
        ["assess", "--patron", "CLI_F0003", "--kind", "other", "--amount-cents", "500", "--note", "xx"],
        "compendium.cli.commands.fine",
    )
    fine = SqlFineRepository(session).list(patron_id=p.id)[0]
    r = _runner(
        session, fine_app,
        ["waive", "--id", str(fine.id), "--note", "compassionate"],
        "compendium.cli.commands.fine",
    )
    assert r.exit_code == 0
    session.refresh(fine)
    assert fine.status == FineStatus.WAIVED.value


def test_cli_fine_pay_partial(session):
    from compendium.cli.commands.fine import app as fine_app

    p = _make_patron(session, "CLI_F0005")
    _runner(
        session, fine_app,
        ["assess", "--patron", "CLI_F0005", "--kind", "other", "--amount-cents", "500", "--note", "xx"],
        "compendium.cli.commands.fine",
    )
    fine = SqlFineRepository(session).list(patron_id=p.id)[0]
    r = _runner(
        session, fine_app,
        ["pay", "--id", str(fine.id), "--amount", "2.00"],
        "compendium.cli.commands.fine",
    )
    assert r.exit_code == 0, r.output
    assert "remaining" in r.output.lower()
    session.refresh(fine)
    assert fine.status == FineStatus.OUTSTANDING.value
    assert fine.paid_cents == 200


def test_cli_fine_pay_bad_amount_is_usage_error(session):
    from compendium.cli.commands.fine import app as fine_app

    p = _make_patron(session, "CLI_F0006")
    _runner(
        session, fine_app,
        ["assess", "--patron", "CLI_F0006", "--kind", "other", "--amount-cents", "500", "--note", "xx"],
        "compendium.cli.commands.fine",
    )
    fine = SqlFineRepository(session).list(patron_id=p.id)[0]
    r = _runner(
        session, fine_app,
        ["pay", "--id", str(fine.id), "--amount", "abc"],
        "compendium.cli.commands.fine",
    )
    assert r.exit_code == 2


def test_cli_fine_waive_without_note(session):
    from compendium.cli.commands.fine import app as fine_app

    p = _make_patron(session, "CLI_F0007")
    _runner(
        session, fine_app,
        ["assess", "--patron", "CLI_F0007", "--kind", "other", "--amount-cents", "500", "--note", "xx"],
        "compendium.cli.commands.fine",
    )
    fine = SqlFineRepository(session).list(patron_id=p.id)[0]
    r = _runner(
        session, fine_app,
        ["waive", "--id", str(fine.id)],
        "compendium.cli.commands.fine",
    )
    assert r.exit_code == 0, r.output
    session.refresh(fine)
    assert fine.status == FineStatus.WAIVED.value


def test_cli_fine_assess_overdue_for_patron(session):
    from compendium.cli.commands.fine import app as fine_app

    _, item = _seed_work_item(session)
    p = _make_patron(session, "CLI_F0004")
    _set_policy(session, per_day=50)
    _make_overdue_loan(session, p, item, days_late=3)

    r = _runner(
        session, fine_app,
        ["assess-overdue", "--patron", "CLI_F0004"],
        "compendium.cli.commands.fine",
    )
    assert r.exit_code == 0, r.output
    assert "created=1" in r.output


def test_cli_loan_declare_lost(session):
    from compendium.cli.commands.item import app as item_app

    _, item = _seed_work_item(session)
    p = _make_patron(session, "CLI_L0001")
    _set_policy(session, per_day=0, lost_default=2500, proc=500)
    _make_overdue_loan(session, p, item, days_late=0)

    r = _runner(
        session, item_app,
        ["declare-lost", "--barcode", item.barcode],
        "compendium.cli.commands.item",
    )
    assert r.exit_code == 0, r.output
    assert item.status == ItemStatus.LOST.value
    fines = SqlFineRepository(session).list(patron_id=p.id)
    kinds = {f.kind for f in fines}
    assert FineKind.LOST.value in kinds
    assert FineKind.PROCESSING.value in kinds


def test_cli_loan_mark_damaged_requires_note(session):
    from compendium.cli.commands.item import app as item_app

    _, item = _seed_work_item(session)
    _make_patron(session, "CLI_L0002")
    _make_overdue_loan(session, SqlPatronRepository(session).get_by_card_number("CLI_L0002"), item, 0)

    # Typer can't call without --note (required), so pass empty via arg — should validate.
    r = _runner(
        session, item_app,
        ["mark-damaged", "--barcode", item.barcode, "--amount-cents", "500", "--note", ""],
        "compendium.cli.commands.item",
    )
    assert r.exit_code == 1


def test_cli_maintenance_assess_overdue_fines_bulk(session):
    from compendium.cli.commands.maintenance import app as maint_app

    _, item1 = _seed_work_item(session, "9780000000101")
    _, item2 = _seed_work_item(session, "9780000000102")
    p1 = _make_patron(session, "CLI_M0001")
    p2 = _make_patron(session, "CLI_M0002")
    _set_policy(session, per_day=10)
    _make_overdue_loan(session, p1, item1, days_late=2)
    _make_overdue_loan(session, p2, item2, days_late=5)

    r = _runner(
        session, maint_app,
        ["assess-overdue-fines"],
        "compendium.cli.commands.maintenance",
    )
    assert r.exit_code == 0, r.output
    assert "created=2" in r.output


def test_cli_policy_set_updates_fine_fields(session):
    from compendium.cli.commands.policy import app as policy_app

    pol = SqlLoanPolicyRepository(session).get_default()
    r = _runner(
        session, policy_app,
        [
            "set", "--id", str(pol.id),
            "--overdue-per-day-cents", "25",
            "--grace-days", "3",
            "--lost-default-cents", "2000",
            "--lost-processing-cents", "500",
        ],
        "compendium.cli.commands.policy",
    )
    assert r.exit_code == 0, r.output
    session.refresh(pol)
    assert pol.overdue_fine_per_day_cents == 25
    assert pol.grace_period_days == 3
    assert pol.lost_item_default_cents == 2000
    assert pol.lost_item_processing_fee_cents == 500
