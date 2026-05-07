"""Bulk metadata refresh — repository iterator + service orchestration.

Patterned after tests/integration/test_refresh_and_enrich.py, which covers
the per-Work refresh path. This file covers the bulk wrapper.
"""

from __future__ import annotations

from unittest.mock import patch

from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditAction, AuditService
from compendium.services.catalog import CatalogService


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

_n = {"i": 0}


def _next_isbn() -> str:
    _n["i"] += 1
    return f"978000000{_n['i']:04d}"


def _catalog(session, *, audit: bool = False) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)) if audit else None,
        source="test",
    )


def _seed(session, **overrides) -> int:
    """Seed a Dune-shaped Work and apply field overrides post-insert.

    `add_from_isbn` doesn't accept a description override directly, so we
    insert via the canonical path then mutate the Work to simulate the
    "missing fields" state we care about. Pass any column name as a keyword
    (including ``isbn=None``) to override after creation.
    """
    isbn = _next_isbn()
    fixture = dict(_DUNE)
    fixture["isbn"] = isbn
    with patch("compendium.services.catalog.lookup_metadata", return_value=fixture):
        work, _ = _catalog(session).add_from_isbn(isbn)
    for key, value in overrides.items():
        setattr(work, key, value)
    session.flush()
    return work.id


# ── iter_for_refresh ─────────────────────────────────────────────────────────


def test_iter_for_refresh_excludes_works_with_no_lookup_key(session):
    repo = SqlWorkRepository(session)
    isbn_id = _seed(session, description="")  # has ISBN, missing description
    keyless_id = _seed(session, isbn=None, upc=None, description="")
    found = {w.id for w in repo.iter_for_refresh()}
    assert isbn_id in found
    assert keyless_id not in found


def test_iter_for_refresh_missing_only_skips_complete_works(session):
    repo = SqlWorkRepository(session)
    complete_id = _seed(session)  # all fields populated by _DUNE
    incomplete_id = _seed(session, description="")
    found = {w.id for w in repo.iter_for_refresh(missing_only=True)}
    assert incomplete_id in found
    assert complete_id not in found


def test_iter_for_refresh_all_returns_complete_and_incomplete(session):
    repo = SqlWorkRepository(session)
    complete_id = _seed(session)
    incomplete_id = _seed(session, description="")
    found = {w.id for w in repo.iter_for_refresh(missing_only=False)}
    assert complete_id in found
    assert incomplete_id in found


def test_iter_for_refresh_limit_caps_results(session):
    _seed(session, description="")
    _seed(session, description="")
    _seed(session, description="")
    repo = SqlWorkRepository(session)
    found = list(repo.iter_for_refresh(limit=2))
    assert len(found) == 2


def test_iter_for_refresh_limit_orders_by_id_for_progress(session):
    repo = SqlWorkRepository(session)
    first_id = _seed(session, description="")
    second_id = _seed(session, description="")
    third_id = _seed(session, description="")
    found = [w.id for w in repo.iter_for_refresh(limit=2)]
    assert found == sorted([first_id, second_id, third_id])[:2]


def test_iter_for_refresh_media_type_filter(session):
    repo = SqlWorkRepository(session)
    book_id = _seed(session, description="")
    found_book = {w.id for w in repo.iter_for_refresh(media_type_code="book")}
    found_dvd = {w.id for w in repo.iter_for_refresh(media_type_code="dvd")}
    assert book_id in found_book
    assert book_id not in found_dvd


def test_iter_for_refresh_missing_fires_on_blank_publisher_or_cover(session):
    repo = SqlWorkRepository(session)
    blank_publisher = _seed(session, publisher="")
    null_publisher = _seed(session, publisher=None)
    blank_cover = _seed(session, cover_image_url="")
    null_cover = _seed(session, cover_image_url=None)
    null_language = _seed(session, language=None)
    found = {w.id for w in repo.iter_for_refresh()}
    for wid in (blank_publisher, null_publisher, blank_cover, null_cover, null_language):
        assert wid in found, f"Work {wid} should match missing-only filter"


# ── refresh_metadata_bulk: aggregation + audit ───────────────────────────────


def test_refresh_metadata_bulk_dry_run_buckets_outcomes(session):
    # One Work missing description (will refresh), one already complete (will
    # be skipped by missing_only=True so it never enters the loop).
    needs_id = _seed(session, description="")
    _seed(session)  # complete; not iterated under missing_only

    fixture = dict(_DUNE)

    def fake_lookup(_media_type, kind, value):
        # Return a fresh description so refresh_metadata sees a planned diff.
        return {**fixture, "isbn": value, "description": "filled in by upstream"}

    with patch("compendium.services.catalog.lookup_metadata", side_effect=fake_lookup):
        report = _catalog(session, audit=True).refresh_metadata_bulk(dry_run=True)

    assert report.dry_run is True
    assert report.total_considered == 1
    assert report.refreshed == 1
    assert report.no_change == 0
    # Dry-run: no audit entry expected.
    audit = AuditService(SqlAuditLogRepository(session))
    bulk_entries = [
        e for e in audit.list(entity_type="work", limit=50)
        if e.action == AuditAction.BULK_REFRESH_METADATA
    ]
    assert bulk_entries == []
    # Dry-run: the Work's description should NOT have been written.
    work = SqlWorkRepository(session).get(needs_id)
    assert work.description == ""


def test_refresh_metadata_bulk_apply_writes_and_audits(session):
    needs_id = _seed(session, description="")
    fixture = dict(_DUNE)

    def fake_lookup(_media_type, kind, value):
        return {**fixture, "isbn": value, "description": "filled in by upstream"}

    with patch("compendium.services.catalog.lookup_metadata", side_effect=fake_lookup):
        report = _catalog(session, audit=True).refresh_metadata_bulk(dry_run=False)

    assert report.refreshed == 1
    assert report.total_considered == 1
    work = SqlWorkRepository(session).get(needs_id)
    assert work.description == "filled in by upstream"

    audit = AuditService(SqlAuditLogRepository(session))
    bulk_entries = [
        e for e in audit.list(entity_type="work", limit=50)
        if e.action == AuditAction.BULK_REFRESH_METADATA
    ]
    assert len(bulk_entries) == 1
    details = bulk_entries[0].details
    assert details["counts"]["refreshed"] == 1
    assert details["counts"]["total_considered"] == 1
    assert details["filters"]["missing_only"] is True


def test_refresh_metadata_bulk_no_change_when_upstream_is_blank(session):
    """Upstream returns the Work's existing values → no fields planned, but
    the refresh did 'find' something. Should bucket as no_change."""
    _seed(session, description="")
    # Fixture intentionally has no description, so _compute_refresh_diff
    # will skip filling it (since `new` is falsy).
    sparse = {**_DUNE, "description": None, "cover_image_url": None}

    def fake_lookup(_media_type, kind, value):
        return {**sparse, "isbn": value}

    with patch("compendium.services.catalog.lookup_metadata", side_effect=fake_lookup):
        report = _catalog(session).refresh_metadata_bulk(dry_run=True)

    assert report.total_considered == 1
    assert report.refreshed == 0
    assert report.no_change == 1


def test_refresh_metadata_bulk_not_found_bucketed(session):
    _seed(session, description="")
    with patch("compendium.services.catalog.lookup_metadata", return_value=None):
        report = _catalog(session).refresh_metadata_bulk(dry_run=True)
    assert report.total_considered == 1
    assert report.not_found == 1
    assert report.refreshed == 0


def test_refresh_metadata_bulk_respects_limit(session):
    _seed(session, description="")
    _seed(session, description="")
    _seed(session, description="")

    def fake_lookup(_media_type, kind, value):
        return {**_DUNE, "isbn": value, "description": "x"}

    with patch("compendium.services.catalog.lookup_metadata", side_effect=fake_lookup):
        report = _catalog(session).refresh_metadata_bulk(limit=2, dry_run=True)
    assert report.total_considered == 2


# ── on_progress callback ─────────────────────────────────────────────────────


def test_refresh_metadata_bulk_on_progress_fires_per_iteration_with_indices(session):
    """Callback receives (index, total, work, per_report) once per Work,
    with index counting from 1 and total matching the candidate count."""
    id_a = _seed(session, description="")
    id_b = _seed(session, description="")

    fixture = dict(_DUNE)

    def fake_lookup(_media_type, _kind, value):
        return {**fixture, "isbn": value, "description": "filled"}

    calls: list[tuple[int, int, int, bool]] = []

    def on_progress(index, total, work, per):
        # Capture (index, total, work_id, planned-was-set) — keep the test
        # tuple JSON-friendly so failures print readably.
        calls.append((index, total, work.id, bool(per and per.planned)))

    with patch("compendium.services.catalog.lookup_metadata", side_effect=fake_lookup):
        _catalog(session).refresh_metadata_bulk(
            dry_run=True, on_progress=on_progress
        )

    assert calls == [(1, 2, id_a, True), (2, 2, id_b, True)]


def test_refresh_metadata_bulk_on_progress_buckets_match_report(session):
    """Each callback invocation's per_report aligns with the bucket the
    aggregate report counted that work into."""
    id_refreshed = _seed(session, description="")
    id_no_change = _seed(session)
    id_not_found = _seed(session, description="")

    fixture = dict(_DUNE)
    refresh_isbn = SqlWorkRepository(session).get(id_refreshed).isbn
    not_found_isbn = SqlWorkRepository(session).get(id_not_found).isbn

    def fake_lookup(_media_type, _kind, value):
        if value == refresh_isbn:
            return {**fixture, "isbn": value, "description": "filled"}
        if value == not_found_isbn:
            return None
        # complete work — return same data as already stored, no diff
        return {**fixture, "isbn": value}

    seen: dict[int, str] = {}

    def on_progress(index, total, work, per):
        # Mirror the bucketing in refresh_metadata_bulk: upstream-miss errors
        # ("Upstream returned no data...") bucket as not_found, missing-key
        # errors as skipped, and a successful refresh with planned changes
        # as refreshed.
        if per is None:
            seen[work.id] = "exception"
        elif per.error:
            if "no ISBN/UPC" in per.error or "no media type" in per.error:
                seen[work.id] = "skipped"
            else:
                seen[work.id] = "not_found"
        elif not per.found:
            seen[work.id] = "not_found"
        elif per.planned:
            seen[work.id] = "refreshed"
        else:
            seen[work.id] = "no_change"

    with patch("compendium.services.catalog.lookup_metadata", side_effect=fake_lookup):
        report = _catalog(session).refresh_metadata_bulk(
            missing_only=False, dry_run=True, on_progress=on_progress
        )

    assert seen[id_refreshed] == "refreshed"
    assert seen[id_no_change] == "no_change"
    assert seen[id_not_found] == "not_found"
    assert report.refreshed == 1
    assert report.no_change == 1
    assert report.not_found == 1
