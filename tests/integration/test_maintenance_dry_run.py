"""--dry-run reports counts and writes nothing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from compendium.cli.main import app
from compendium.config.seed import seed_defaults
from compendium.domain.enums import HoldStatus
from compendium.domain.models import (
    Base,
    Branch,
    Creator,
    DeletedEntity,
    Hold,
    Item,
    MediaType,
    Patron,
    Work,
    WorkCreator,
)
from compendium.services import site_settings as ss

runner = CliRunner()


@pytest.fixture
def cli_db(monkeypatch):
    """Route every command's session_scope() at a shared in-memory DB.

    Copied from ``tests/integration/test_maintenance_quiet.py`` — these tests
    invoke the CLI directly with no per-command patching, so each command's
    own ``session_scope()`` call must resolve to the same engine. StaticPool
    keeps the single in-memory connection alive across those separate
    sessions.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
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
def trashed_work_older_than_retention(cli_db):
    """Create + delete a work, then backdate deleted_entity.deleted_at well
    beyond trash_retention_days so purge-trash's default window catches it."""
    factory = sessionmaker(bind=cli_db, autoflush=False, expire_on_commit=False)
    session = factory()
    branch = session.query(Branch).first()
    media = session.query(MediaType).filter_by(code="book").first()
    creator = Creator(display_name="Frank Herbert", sort_name="Herbert, Frank")
    work = Work(
        title="Dune", media_type_id=media.id, isbn="9780000000001", search_text="Dune"
    )
    work.creators.append(WorkCreator(creator=creator, role="author", display_order=0))
    session.add(work)
    session.flush()
    session.add(
        Item(
            work_id=work.id,
            branch_id=branch.id,
            barcode="BC-DRY-1",
            accession_number="ACC-DRY-1",
        )
    )
    session.commit()
    work_id = work.id
    session.close()

    result = runner.invoke(app, ["work", "delete", str(work_id), "--yes"])
    assert result.exit_code == 0, result.output

    session = factory()
    trash_row = session.query(DeletedEntity).order_by(DeletedEntity.id.desc()).first()
    trash_row.deleted_at = datetime.now(timezone.utc) - timedelta(days=9999)
    session.commit()
    session.close()


@pytest.fixture
def expired_waiting_hold(cli_db):
    """Place a WAITING hold with expires_at in the past."""
    factory = sessionmaker(bind=cli_db, autoflush=False, expire_on_commit=False)
    session = factory()
    branch = session.query(Branch).first()
    media = session.query(MediaType).filter_by(code="book").first()
    work = Work(title="Hold Me", media_type_id=media.id, search_text="Hold Me")
    session.add(work)
    session.flush()
    session.add(
        Patron(library_card_number="CARD-DRY-1", full_name="Pat Ron")
    )
    session.flush()
    patron = session.query(Patron).filter_by(library_card_number="CARD-DRY-1").one()
    session.add(
        Hold(
            work_id=work.id,
            patron_id=patron.id,
            branch_id=branch.id,
            status=HoldStatus.WAITING.value,
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    session.commit()
    session.close()


def test_purge_trash_dry_run_keeps_rows(cli_db, trashed_work_older_than_retention):
    result = runner.invoke(app, ["maintenance", "purge-trash", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Would purge 1" in result.output
    listed = runner.invoke(app, ["work", "trash", "list"])
    assert "deleted" in listed.output.lower() or "Dune" in listed.output


def test_expire_holds_dry_run(cli_db, expired_waiting_hold):
    result = runner.invoke(app, ["maintenance", "expire-holds", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Would expire 1" in result.output
    again = runner.invoke(app, ["maintenance", "expire-holds", "--dry-run"])
    assert "Would expire 1" in again.output  # nothing was written


def test_prune_metadata_cache_dry_run(cli_db):
    result = runner.invoke(app, ["maintenance", "prune-metadata-cache", "--dry-run"])
    assert result.exit_code == 0  # empty cache: "no expired entries" path


def test_prune_cover_cache_dry_run(cli_db, monkeypatch, tmp_path):
    monkeypatch.setenv("COMPENDIUM_COVER_CACHE_DIR", str(tmp_path / "covers"))
    result = runner.invoke(app, ["maintenance", "prune-cover-cache", "--dry-run"])
    assert result.exit_code == 0
