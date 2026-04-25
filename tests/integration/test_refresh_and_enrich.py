"""Tests for ``CatalogService.refresh_metadata`` and import enrichment.

Covers:
- Refresh dry-run computes diff with fill-missing semantics for text fields
  and unconditional replacement for the cover URL.
- Refresh apply commits the changes, invalidates the cover proxy cache,
  and force-bumps work.updated_at.
- "URL unchanged but cache busted" — apply still invalidates the proxy
  file even when the cover URL didn't change.
- Refresh on a work without a lookup key returns a friendly error.
- Refresh on a not-found upstream returns ``found=False`` without raising.
- Import: ``--enrich`` off (default) does not call the metadata source.
- Import: ``--enrich`` on fills missing fields when ISBN is present and
  preserves CSV-supplied values.
- ``cover_image_url`` round-trips via export → import.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from compendium.domain.errors import ExternalLookupError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
from compendium.services.catalog import CatalogService
from compendium.services.import_export import (
    ExportFilters,
    ExportService,
    ImportOptions,
    ImportService,
)


_DUNE = {
    "title": "Dune",
    "subtitle": None,
    "authors": ["Frank Herbert"],
    "creator_role": "author",
    "publisher": "Chilton Books",
    "publication_year": 1965,
    "description": "Sci-fi epic on Arrakis.",
    "cover_image_url": "https://covers.openlibrary.org/b/id/12345-L.jpg",
    "isbn": "9780441013593",
    "upc": None,
    "external_ids": {"openlibrary": "OL1234W"},
    "extra_metadata": {},
}


def _catalog(session, audit=False):
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)) if audit else None,
        source="test",
    )


_n = {"i": 0}


def _next_isbn() -> str:
    _n["i"] += 1
    return f"978000000{_n['i']:04d}"


def _seed_dune(session, *, with_cover: bool = True, isbn: str | None = None) -> int:
    """Insert a Dune-shaped work via add_from_isbn. Uses a unique synthetic
    ISBN per call so multiple tests in the same suite don't collide on the
    shared in-memory engine."""
    isbn = isbn or _next_isbn()
    fixture = dict(_DUNE)
    fixture["isbn"] = isbn
    if not with_cover:
        fixture["cover_image_url"] = None
    with patch(
        "compendium.services.catalog.lookup_metadata", return_value=fixture
    ):
        work, _ = _catalog(session).add_from_isbn(isbn)
    session.flush()
    return work.id


# ── Refresh: dry-run / fill-missing / cover-replace ──────────────────────────


def test_refresh_dry_run_no_diff_when_nothing_missing(session):
    work_id = _seed_dune(session)
    work = SqlWorkRepository(session).get(work_id)
    fixture = dict(_DUNE)
    fixture["isbn"] = work.isbn
    with patch(
        "compendium.services.catalog.lookup_metadata", return_value=fixture
    ):
        report = _catalog(session).refresh_metadata(work_id, dry_run=True)
    assert report.found is True
    assert report.applied is False
    assert report.planned == {}


def test_refresh_dry_run_fills_missing_text_field(session):
    work_id = _seed_dune(session)
    work = SqlWorkRepository(session).get(work_id)
    work.description = None
    session.flush()
    fixture = dict(_DUNE)
    fixture["isbn"] = work.isbn

    with patch(
        "compendium.services.catalog.lookup_metadata", return_value=fixture
    ):
        report = _catalog(session).refresh_metadata(work_id, dry_run=True)
    assert "description" in report.planned
    old, new = report.planned["description"]
    assert old is None
    assert new == _DUNE["description"]


def test_refresh_dry_run_does_not_overwrite_existing_text_field(session):
    work_id = _seed_dune(session)
    work = SqlWorkRepository(session).get(work_id)
    work.description = "Librarian's careful summary."
    session.flush()
    fixture = dict(_DUNE)
    fixture["isbn"] = work.isbn

    with patch(
        "compendium.services.catalog.lookup_metadata", return_value=fixture
    ):
        report = _catalog(session).refresh_metadata(work_id, dry_run=True)
    assert "description" not in report.planned


def test_refresh_dry_run_replaces_cover_when_upstream_differs(session):
    work_id = _seed_dune(session)
    work = SqlWorkRepository(session).get(work_id)
    fixture = dict(_DUNE)
    fixture["isbn"] = work.isbn
    fixture["cover_image_url"] = "https://covers.openlibrary.org/b/id/99999-L.jpg"

    with patch(
        "compendium.services.catalog.lookup_metadata", return_value=fixture
    ):
        report = _catalog(session).refresh_metadata(work_id, dry_run=True)
    assert "cover_image_url" in report.planned


def test_refresh_apply_commits_and_invalidates_cache(session, tmp_path, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_COVER_CACHE_DIR", str(tmp_path))
    work_id = _seed_dune(session)
    work = SqlWorkRepository(session).get(work_id)
    work.description = None
    session.flush()
    pre_updated_at = work.updated_at

    new_url = "https://covers.openlibrary.org/b/id/55555-L.jpg"
    fixture = dict(_DUNE)
    fixture["isbn"] = work.isbn
    fixture["cover_image_url"] = new_url

    from compendium.services import covers as _covers

    old_path = _covers.cache_dir() / f"{_covers.cache_key(_DUNE['cover_image_url'])}.jpg"
    old_path.write_bytes(b"old-bytes")
    new_path = _covers.cache_dir() / f"{_covers.cache_key(new_url)}.jpg"
    new_path.write_bytes(b"new-bytes")

    with patch(
        "compendium.services.catalog.lookup_metadata", return_value=fixture
    ):
        report = _catalog(session, audit=True).refresh_metadata(
            work_id, dry_run=False
        )
    session.flush()

    assert report.applied is True
    assert report.cover_cache_busted is True
    assert not old_path.exists()
    assert not new_path.exists()

    refreshed = SqlWorkRepository(session).get(work_id)
    assert refreshed.cover_image_url == new_url
    assert refreshed.description == _DUNE["description"]
    assert refreshed.updated_at >= pre_updated_at


def test_refresh_apply_busts_cache_even_when_url_unchanged(
    session, tmp_path, monkeypatch
):
    monkeypatch.setenv("COMPENDIUM_COVER_CACHE_DIR", str(tmp_path))
    work_id = _seed_dune(session)
    work = SqlWorkRepository(session).get(work_id)
    fixture = dict(_DUNE)
    fixture["isbn"] = work.isbn

    from compendium.services import covers as _covers

    cached = _covers.cache_dir() / f"{_covers.cache_key(_DUNE['cover_image_url'])}.jpg"
    cached.write_bytes(b"stale")

    with patch(
        "compendium.services.catalog.lookup_metadata", return_value=fixture
    ):
        report = _catalog(session, audit=True).refresh_metadata(
            work_id, dry_run=False
        )
    session.flush()

    assert report.applied is True
    assert report.cover_cache_busted is True
    assert not cached.exists()


def test_refresh_emits_audit_on_apply(session):
    work_id = _seed_dune(session)
    work = SqlWorkRepository(session).get(work_id)
    work.description = None
    session.flush()
    fixture = dict(_DUNE)
    fixture["isbn"] = work.isbn

    audit = AuditService(SqlAuditLogRepository(session))
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=audit,
        source="test",
    )
    with patch(
        "compendium.services.catalog.lookup_metadata", return_value=fixture
    ):
        catalog.refresh_metadata(work_id, dry_run=False)
    session.flush()

    entries = audit.list(entity_type=AuditEntityType.WORK)
    refresh_entries = [
        e for e in entries
        if (e.details or {}).get("refreshed_from") == "openlibrary"
    ]
    assert refresh_entries
    assert "description" in refresh_entries[0].details["fields_updated"]


def test_refresh_no_lookup_key_returns_error(session):
    catalog = _catalog(session)
    work, _ = catalog.add_manual("book", title="Self-Published Zine")
    session.flush()

    report = catalog.refresh_metadata(work.id, dry_run=True)
    assert report.found is False
    assert "external identifier" in (report.error or "").lower()


def test_refresh_upstream_error_returns_error_report(session):
    work_id = _seed_dune(session)
    with patch(
        "compendium.services.catalog.lookup_metadata",
        side_effect=ExternalLookupError("openlibrary unreachable"),
    ):
        report = _catalog(session).refresh_metadata(work_id, dry_run=True)
    assert report.found is False
    assert "openlibrary unreachable" in (report.error or "")


# ── Cover proxy invalidate() ─────────────────────────────────────────────────


def test_invalidate_removes_jpg_and_404_sentinel(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_COVER_CACHE_DIR", str(tmp_path))
    from compendium.services import covers as _covers

    url = "https://covers.openlibrary.org/b/id/77777-L.jpg"
    key = _covers.cache_key(url)
    jpg = _covers.cache_dir() / f"{key}.jpg"
    sentinel = _covers.cache_dir() / f"{key}.404"
    jpg.write_bytes(b"x")
    sentinel.write_bytes(b"")

    assert _covers.invalidate(url) is True
    assert not jpg.exists()
    assert not sentinel.exists()


def test_invalidate_safe_when_nothing_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_COVER_CACHE_DIR", str(tmp_path))
    from compendium.services import covers as _covers

    assert _covers.invalidate("https://covers.openlibrary.org/b/id/none-L.jpg") is False


# ── Import enrichment ───────────────────────────────────────────────────────


def _import_services(session):
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )
    importer = ImportService(
        session=session,
        catalog=catalog,
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        source="test",
    )
    exporter = ExportService(work_repo=SqlWorkRepository(session))
    return importer, exporter


def _csv_with_isbn(isbn: str, *, cover_url: str = "", publisher: str = "") -> str:
    return (
        "media_type,title,authors,isbn,publisher,cover_image_url\n"
        f"book,Dune,Frank Herbert,{isbn},{publisher},{cover_url}\n"
    )


def test_import_enrich_off_skips_lookup(session):
    importer, _ = _import_services(session)
    isbn = _next_isbn()
    with patch(
        "compendium.services.metadata.lookup_metadata"
    ) as mock_lookup:
        report = importer.import_csv(io.StringIO(_csv_with_isbn(isbn)), ImportOptions())
    assert report.created_works == 1
    assert report.enriched_rows == 0
    mock_lookup.assert_not_called()
    work = SqlWorkRepository(session).get_by_isbn(isbn)
    assert work.cover_image_url is None or work.cover_image_url == ""
    assert work.description is None


def test_import_enrich_on_fills_missing_fields(session):
    importer, _ = _import_services(session)
    isbn = _next_isbn()
    fixture = dict(_DUNE)
    fixture["isbn"] = isbn
    with patch(
        "compendium.services.metadata.lookup_metadata", return_value=fixture
    ):
        report = importer.import_csv(
            io.StringIO(_csv_with_isbn(isbn)),
            ImportOptions(enrich_from_external=True),
        )
    assert report.created_works == 1
    assert report.enriched_rows == 1
    work = SqlWorkRepository(session).get_by_isbn(isbn)
    assert work.cover_image_url == _DUNE["cover_image_url"]
    assert work.description == _DUNE["description"]
    assert work.publisher == _DUNE["publisher"]


def test_import_enrich_does_not_overwrite_csv_supplied_value(session):
    importer, _ = _import_services(session)
    isbn = _next_isbn()
    fixture = dict(_DUNE)
    fixture["isbn"] = isbn
    csv_body = _csv_with_isbn(isbn, publisher="Berkley Books")
    with patch(
        "compendium.services.metadata.lookup_metadata", return_value=fixture
    ):
        importer.import_csv(
            io.StringIO(csv_body),
            ImportOptions(enrich_from_external=True),
        )
    work = SqlWorkRepository(session).get_by_isbn(isbn)
    assert work.publisher == "Berkley Books"
    assert work.description == _DUNE["description"]


def test_cover_image_url_round_trips_via_csv(session):
    importer, exporter = _import_services(session)
    isbn = _next_isbn()
    csv_body = _csv_with_isbn(isbn, cover_url="https://example.test/dune.jpg")
    importer.import_csv(io.StringIO(csv_body), ImportOptions())

    out = io.StringIO()
    exporter.export_csv(out, ExportFilters(media_type_code="book"))
    body = out.getvalue()
    assert "cover_image_url" in body.splitlines()[0]
    assert "https://example.test/dune.jpg" in body
