"""CLI smoke for ``compendium maintenance refresh-metadata``.

Exercises the Typer command end-to-end with a session bound via
``session_scope`` and ``lookup_metadata`` monkeypatched. Other coverage
(filter logic, aggregation buckets, audit) lives in
tests/integration/test_catalog_refresh_bulk.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from typer.testing import CliRunner

from compendium.cli.commands.maintenance import app as maintenance_app
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.services.audit import AuditService


_DUNE = {
    "title": "Dune",
    "subtitle": None,
    "authors": ["Frank Herbert"],
    "creator_role": "author",
    "publisher": "Chilton Books",
    "publication_year": 1965,
    "description": "Sci-fi epic on Arrakis.",
    "cover_image_url": "https://covers.openlibrary.org/b/id/12345-L.jpg",
    "language": "en",
    "isbn": "9780441013593",
    "upc": None,
    "external_ids": {"openlibrary": "OL1234W"},
    "extra_metadata": {},
}


def _seed_incomplete(session, isbn: str = "9780441013594") -> int:
    fixture = dict(_DUNE)
    fixture["isbn"] = isbn
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        source="test",
    )
    with patch("compendium.services.catalog.lookup_metadata", return_value=fixture):
        work, _ = catalog.add_from_isbn(isbn)
    work.description = ""
    session.flush()
    return work.id


def _run_cli(session, args):
    @contextmanager
    def _scope():
        yield session

    runner = CliRunner()
    with patch(
        "compendium.cli.commands.maintenance.session_scope", _scope
    ):
        return runner.invoke(maintenance_app, args)


def test_cli_refresh_metadata_dry_run_reports_no_writes(session):
    work_id = _seed_incomplete(session)
    fixture = dict(_DUNE)

    def fake_lookup(_media_type, _kind, value):
        return {**fixture, "isbn": value, "description": "filled in"}

    with patch(
        "compendium.services.catalog.lookup_metadata", side_effect=fake_lookup
    ):
        result = _run_cli(session, ["refresh-metadata", "--dry-run", "--limit", "5"])

    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "considered  : 1" in out
    assert "refreshed   : 1" in out
    assert "dry-run" in out

    work = SqlWorkRepository(session).get(work_id)
    assert work.description == ""  # unchanged in dry-run


def test_cli_refresh_metadata_apply_writes_changes(session):
    work_id = _seed_incomplete(session, isbn="9780441013596")
    fixture = dict(_DUNE)

    def fake_lookup(_media_type, _kind, value):
        return {**fixture, "isbn": value, "description": "filled in by upstream"}

    with patch(
        "compendium.services.catalog.lookup_metadata", side_effect=fake_lookup
    ):
        result = _run_cli(session, ["refresh-metadata", "--limit", "5"])

    assert result.exit_code == 0, result.output
    work = SqlWorkRepository(session).get(work_id)
    assert work.description == "filled in by upstream"


def test_cli_refresh_metadata_zero_eligible_is_clean_exit(session):
    # No Works seeded → eligible-set empty.
    result = _run_cli(session, ["refresh-metadata", "--dry-run"])
    assert result.exit_code == 0
    assert "considered  : 0" in result.output


def test_cli_refresh_metadata_all_flag_includes_complete_works(session):
    # Seed one fully complete Work.
    isbn = "9780441013597"
    fixture = dict(_DUNE)
    fixture["isbn"] = isbn
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )
    with patch("compendium.services.catalog.lookup_metadata", return_value=fixture):
        catalog.add_from_isbn(isbn)
    session.flush()

    with patch(
        "compendium.services.catalog.lookup_metadata", return_value=fixture
    ):
        result = _run_cli(session, ["refresh-metadata", "--all", "--dry-run"])

    assert result.exit_code == 0, result.output
    # --all: even already-complete works are considered (no_change bucket).
    assert "considered  : 1" in result.output
    assert "no change   : 1" in result.output
