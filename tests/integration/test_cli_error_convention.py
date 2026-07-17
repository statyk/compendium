"""CLI-wide conventions verified by Slice 4 Task 9:

- Every user-facing error goes to stderr, prefixed with ``Error: ``.
- ``--limit`` list commands print a truncation notice on stderr when the
  limit is hit.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from compendium.cli.main import app
from compendium.config.seed import seed_defaults
from compendium.domain.models import Base
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services import site_settings as ss
from compendium.services.audit import AuditService
from compendium.services.patrons import PatronService
from tests.helpers import setup_sqlite_fts

runner = CliRunner()


@pytest.fixture
def cli_db(monkeypatch):
    """Route every command's session_scope() at a shared in-memory DB.

    Mirrors the fixture in test_cli_confirmations.py: each command's own
    session_scope() call must resolve to the same engine, so StaticPool
    keeps the single in-memory connection alive across separate sessions.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    seed_session = factory()
    seed_defaults(seed_session)
    seed_session.commit()
    seed_session.close()

    monkeypatch.setattr("compendium.db.engine.get_engine", lambda: engine)
    monkeypatch.setattr("compendium.db.session.get_engine", lambda: engine)
    ss.invalidate_cache()
    yield engine
    ss.invalidate_cache()


@pytest.fixture
def thirty_patrons(cli_db):
    """Seed 30 patrons directly via PatronService so `patron list --limit 10` truncates."""
    factory = sessionmaker(bind=cli_db, autoflush=False, expire_on_commit=False)
    session = factory()
    svc = PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label="test",
        source="cli",
    )
    for i in range(30):
        svc.create(full_name=f"Test Patron {i:02d}")
    session.commit()
    session.close()


@pytest.fixture
def item_with_loan_history(cli_db):
    """Seed a single item with more loan history rows than a small --limit.

    Cycles checkout/checkin on one item several times so `loan item-history`
    has more rows than a deliberately small --limit.
    """
    from compendium.domain.models import Branch, Item, MediaType, Patron, Work
    from compendium.repositories.sql.branch_repository import SqlBranchRepository
    from compendium.repositories.sql.item_repository import SqlItemRepository
    from compendium.repositories.sql.loan_policy_repository import (
        SqlLoanPolicyRepository,
    )
    from compendium.services.circulation import CirculationService

    factory = sessionmaker(bind=cli_db, autoflush=False, expire_on_commit=False)
    session = factory()

    media_type = session.query(MediaType).filter_by(code="book").one()
    branch = session.query(Branch).filter_by(code="MAIN").one()
    work = Work(title="Loan History Test", media_type_id=media_type.id)
    session.add(work)
    session.flush()
    item = Item(
        work_id=work.id,
        branch_id=branch.id,
        barcode="LOANHIST01",
        accession_number="ACC-LOANHIST01",
    )
    session.add(item)
    patron = Patron(library_card_number="LHCARD", full_name="Loop Patron")
    session.add(patron)
    session.flush()
    session.commit()

    circulation = CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )
    for _ in range(8):
        circulation.checkout(item.barcode, patron.library_card_number)
        circulation.checkin(item.barcode)
    session.commit()
    barcode = item.barcode
    session.close()
    return barcode


def test_error_goes_to_stderr_with_prefix(cli_db):
    result = runner.invoke(app, ["item", "show", "NO-SUCH-BARCODE"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Error: ")


def test_role_edit_no_flags_uses_error_prefix(cli_db):
    result = runner.invoke(app, ["role", "edit", "--id", "1"])
    assert result.exit_code in (1, 2)
    assert "Error: " in result.stderr


def test_patron_list_truncation_notice(cli_db, thirty_patrons):
    result = runner.invoke(app, ["patron", "list", "--limit", "10"])
    assert result.exit_code == 0
    assert "Showing first 10 row(s)" in result.stderr


def test_loan_item_history_truncation_notice(cli_db, item_with_loan_history):
    result = runner.invoke(
        app,
        ["loan", "item-history", "--barcode", item_with_loan_history, "--limit", "5"],
    )
    assert result.exit_code == 0
    assert "Showing first 5 row(s)" in result.stderr


def test_patron_list_offset_pages_past_first_batch(cli_db, thirty_patrons):
    first = runner.invoke(app, ["patron", "list", "--limit", "10"])
    second = runner.invoke(app, ["patron", "list", "--limit", "10", "--offset", "10"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.stdout != second.stdout


@pytest.fixture
def fifteen_works(cli_db):
    """Seed 15 works sharing a distinctive title token, for `work search` truncation."""
    from compendium.repositories.sql.branch_repository import SqlBranchRepository
    from compendium.repositories.sql.counters import SqlCounterRepository
    from compendium.repositories.sql.creator_repository import SqlCreatorRepository
    from compendium.repositories.sql.item_repository import SqlItemRepository
    from compendium.repositories.sql.media_type_repository import (
        SqlMediaTypeRepository,
    )
    from compendium.repositories.sql.work_repository import SqlWorkRepository
    from compendium.services.catalog import CatalogService

    factory = sessionmaker(bind=cli_db, autoflush=False, expire_on_commit=False)
    session = factory()
    svc = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        counter_repo=SqlCounterRepository(session),
    )
    for i in range(15):
        svc.add_manual("book", f"Zephyrwood Chronicles {i:02d}")
    session.commit()
    session.close()


@pytest.fixture
def fifteen_fines(cli_db):
    """Seed one patron with 15 manually-assessed fines, for `fine list` truncation."""
    factory = sessionmaker(bind=cli_db, autoflush=False, expire_on_commit=False)
    session = factory()
    svc = PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label="test",
        source="cli",
    )
    patron = svc.create(full_name="Fine Patron")
    session.commit()
    card = patron.library_card_number
    session.close()

    for _ in range(15):
        result = runner.invoke(
            app,
            [
                "fine", "assess",
                "--patron", card,
                "--kind", "other",
                "--amount-cents", "100",
                "--note", "test fine",
            ],
        )
        assert result.exit_code == 0, result.stderr


def test_work_search_truncation_notice_present_at_limit(cli_db, fifteen_works):
    result = runner.invoke(
        app, ["work", "search", "Zephyrwood", "--limit", "10"]
    )
    assert result.exit_code == 0
    assert "Showing first 10 row(s)" in result.stderr


def test_work_search_truncation_notice_absent_under_limit(cli_db, fifteen_works):
    result = runner.invoke(
        app, ["work", "search", "Zephyrwood", "--limit", "50"]
    )
    assert result.exit_code == 0
    assert result.stderr == ""


def test_fine_list_truncation_notice_present_at_limit(cli_db, fifteen_fines):
    result = runner.invoke(app, ["fine", "list", "--limit", "10"])
    assert result.exit_code == 0
    assert "Showing first 10 row(s)" in result.stderr


def test_fine_list_truncation_notice_absent_under_limit(cli_db, fifteen_fines):
    result = runner.invoke(app, ["fine", "list", "--limit", "50"])
    assert result.exit_code == 0
    assert result.stderr == ""
