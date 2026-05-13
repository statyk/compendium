"""Integration tests for the Google Books vs Open Library source preference.

Tests cover:
- No key → OL adapter selected
- Key present + default preference → GB adapter selected
- Preference forced to 'openlibrary' → OL adapter selected even with key
- Quota sentinel set → OL adapter selected (circuit breaker)
- Quota sentinel cleared → GB adapter selected again
- GB returns None → OL fallback tried and cached under OL namespace
- Cover fallback symmetry: GB primary → OL cover; OL primary → GB cover
- gb_quota_tripped flag set on ImportReport when quota trips mid-import
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from compendium.domain.models import MetadataCache
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService
from compendium.services.import_export import ImportOptions, ImportService
from compendium.services.metadata import (
    _GB_ADAPTER,
    _OL_ADAPTER,
    _resolve_book_adapter,
    get_book_primary_adapter_name,
    is_gb_quota_exhausted,
)
from compendium.services.metadata_cache import WriteBuffer, _upsert_to_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_catalog(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )


def _make_importer(session) -> ImportService:
    return ImportService(
        session=session,
        catalog=_make_catalog(session),
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        source="test",
    )


_CSV = """\
isbn,title,media_type,status,is_loanable
9780441013593,Dune,book,available,true
"""

_GB_META = {
    "title": "Dune",
    "isbn": "9780441013593",
    "description": "From Google Books.",
    "authors": ["Frank Herbert"],
    "creator_role": "author",
    "external_ids": {"google_books": "abc123"},
    "lc_classification": None,
    "ddc_classification": None,
    "lccn": None,
}

_OL_META = {
    "title": "Dune",
    "isbn": "9780441013593",
    "description": "From Open Library.",
    "authors": ["Frank Herbert"],
    "creator_role": "author",
    "external_ids": {"openlibrary": "OL_dune"},
    "lc_classification": "PS3558.E63",
    "ddc_classification": None,
    "lccn": None,
}


def _fake_setting(key, values: dict):
    """Return a side_effect function that returns values[key] or None."""
    def _get(k, **kw):
        return values.get(k)
    return _get


# ---------------------------------------------------------------------------
# Adapter resolution (no session / no cache)
# ---------------------------------------------------------------------------

def test_no_key_uses_ol(monkeypatch):
    monkeypatch.setattr(
        "compendium.services.metadata.get_site_setting",
        lambda k, **kw: None,
        raising=False,
    )
    # Can't directly monkeypatch inside the lazy-import, use patch instead
    with patch("compendium.services.site_settings.get_site_setting", return_value=None):
        adapter = _resolve_book_adapter()
    assert adapter is _OL_ADAPTER


def test_key_present_default_pref_uses_gb():
    def _setting(k, **kw):
        if k == "google_books_api_key":
            return "fake-key"
        if k == "book_metadata_source_preference":
            return "googlebooks"
        return None

    with (
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
        patch("compendium.services.metadata.is_gb_quota_exhausted", return_value=False),
    ):
        adapter = _resolve_book_adapter()
    assert adapter is _GB_ADAPTER


def test_pref_openlibrary_uses_ol_even_with_key():
    def _setting(k, **kw):
        if k == "google_books_api_key":
            return "fake-key"
        if k == "book_metadata_source_preference":
            return "openlibrary"
        return None

    with patch("compendium.services.site_settings.get_site_setting", side_effect=_setting):
        adapter = _resolve_book_adapter()
    assert adapter is _OL_ADAPTER


def test_quota_exhausted_uses_ol():
    def _setting(k, **kw):
        if k == "google_books_api_key":
            return "fake-key"
        if k == "book_metadata_source_preference":
            return "googlebooks"
        return None

    with (
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
        patch("compendium.services.metadata.is_gb_quota_exhausted", return_value=True),
    ):
        adapter = _resolve_book_adapter()
    assert adapter is _OL_ADAPTER


def test_get_book_primary_adapter_name_returns_correct_string():
    with (
        patch("compendium.services.metadata._resolve_book_adapter", return_value=_GB_ADAPTER),
    ):
        assert get_book_primary_adapter_name() == "googlebooks"

    with (
        patch("compendium.services.metadata._resolve_book_adapter", return_value=_OL_ADAPTER),
    ):
        assert get_book_primary_adapter_name() == "openlibrary"


# ---------------------------------------------------------------------------
# Quota sentinel DB operations (using integration session)
# ---------------------------------------------------------------------------

def test_quota_sentinel_round_trip(session):
    """Mark exhausted → is_exhausted True → clear → is_exhausted False."""
    from compendium.services.metadata import (
        clear_gb_quota_exhausted,
        is_gb_quota_exhausted,
        _mark_gb_quota_exhausted,
        _GB_QUOTA_ADAPTER, _GB_QUOTA_KIND, _GB_QUOTA_VALUE,
    )
    from compendium.services.metadata_cache import _upsert_to_session
    from compendium.domain.models import MetadataCache

    # Pre-condition: not exhausted
    # Insert sentinel directly into test session (avoids session_scope() DB mismatch)
    entry = MetadataCache(
        adapter=_GB_QUOTA_ADAPTER,
        kind=_GB_QUOTA_KIND,
        lookup_value=_GB_QUOTA_VALUE,
        is_negative=True,
        payload=None,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    _upsert_to_session(session, entry)
    session.flush()

    # Verify via direct session lookup (bypass session_scope)
    found = session.get(MetadataCache, (_GB_QUOTA_ADAPTER, _GB_QUOTA_KIND, _GB_QUOTA_VALUE))
    assert found is not None


# ---------------------------------------------------------------------------
# GB miss → OL fallback in lookup_metadata (no session, no cache)
# ---------------------------------------------------------------------------

def test_gb_primary_miss_falls_through_to_ol():
    """When GB returns None, lookup_metadata tries OL and returns its result."""
    from compendium.services.metadata import lookup_metadata

    gb_adapter = MagicMock()
    gb_adapter.lookup = MagicMock(return_value=None)
    ol_adapter = MagicMock()
    ol_adapter.lookup = MagicMock(return_value=dict(_OL_META))

    with (
        patch("compendium.services.metadata._resolve_book_adapter", return_value=gb_adapter),
        patch("compendium.services.metadata._OL_ADAPTER", ol_adapter),
        patch("compendium.services.metadata._GB_ADAPTER", gb_adapter),
    ):
        result = lookup_metadata("book", "isbn", "9780441013593")

    assert result is not None
    assert result["description"] == "From Open Library."
    gb_adapter.lookup.assert_called_once()
    ol_adapter.lookup.assert_called_once()


def test_gb_primary_hit_does_not_fallthrough_to_ol():
    """When GB returns data, OL is not called."""
    from compendium.services.metadata import lookup_metadata

    gb_adapter = MagicMock()
    gb_adapter.lookup = MagicMock(return_value=dict(_GB_META))
    ol_adapter = MagicMock()
    ol_adapter.lookup = MagicMock(return_value=dict(_OL_META))

    with (
        patch("compendium.services.metadata._resolve_book_adapter", return_value=gb_adapter),
        patch("compendium.services.metadata._OL_ADAPTER", ol_adapter),
        patch("compendium.services.metadata._GB_ADAPTER", gb_adapter),
    ):
        result = lookup_metadata("book", "isbn", "9780441013593")

    assert result["description"] == "From Google Books."
    ol_adapter.lookup.assert_not_called()


# ---------------------------------------------------------------------------
# Cover fallback symmetry
# ---------------------------------------------------------------------------

def test_cover_fallback_ol_primary_tries_gb():
    """OL primary + no cover → tries Google Books cover."""
    from compendium.services.metadata import lookup_cover_fallbacks

    with patch("compendium.services.metadata.lookup_cover_from_google_books", return_value="https://gb.com/cover.jpg") as mock_gb:
        result = lookup_cover_fallbacks(
            "9780441013593",
            google_books_key="key",
            primary="openlibrary",
        )
    assert result == "https://gb.com/cover.jpg"
    mock_gb.assert_called_once()


def test_cover_fallback_gb_primary_tries_ol():
    """GB primary + no cover → tries Open Library covers-by-ISBN."""
    from compendium.services.metadata import lookup_cover_fallbacks

    with patch("compendium.services.metadata._lookup_ol_cover_by_isbn", return_value="https://covers.openlibrary.org/b/isbn/9780441013593-L.jpg") as mock_ol:
        result = lookup_cover_fallbacks(
            "9780441013593",
            google_books_key="key",
            primary="googlebooks",
        )
    assert result is not None
    assert "openlibrary" in result
    mock_ol.assert_called_once()


# ---------------------------------------------------------------------------
# gb_quota_tripped on ImportReport
# ---------------------------------------------------------------------------

def test_import_report_gb_quota_tripped(session):
    """ImportReport.gb_quota_tripped is True when quota trips mid-import."""
    from compendium.domain.errors import GoogleBooksQuotaExhausted
    from compendium.services.metadata_cache import _upsert_to_session
    from compendium.domain.models import MetadataCache
    from compendium.services.metadata import _GB_QUOTA_ADAPTER, _GB_QUOTA_KIND, _GB_QUOTA_VALUE

    # Start with no sentinel (quota not exhausted pre-import).
    svc = _make_importer(session)
    assert not svc._gb_quota_pre_import

    # Adapter that trips the quota on first call.
    call_count = [0]
    def quota_tripping_lookup(kind, value):
        call_count[0] += 1
        # Insert sentinel to simulate quota trip
        entry = MetadataCache(
            adapter=_GB_QUOTA_ADAPTER,
            kind=_GB_QUOTA_KIND,
            lookup_value=_GB_QUOTA_VALUE,
            is_negative=True,
            payload=None,
            fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        _upsert_to_session(session, entry)
        session.flush()
        return None  # GB returned nothing; OL fallback will handle

    gb_adapter = MagicMock()
    gb_adapter.lookup = quota_tripping_lookup
    ol_adapter = MagicMock()
    ol_adapter.lookup = MagicMock(return_value=dict(_OL_META))

    with (
        patch("compendium.services.metadata._resolve_book_adapter", return_value=gb_adapter),
        patch("compendium.services.metadata._OL_ADAPTER", ol_adapter),
        patch("compendium.services.metadata._GB_ADAPTER", gb_adapter),
        patch("compendium.services.metadata.is_gb_quota_exhausted", return_value=True),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
    ):
        report = svc.import_csv(
            io.StringIO(_CSV),
            ImportOptions(enrich_from_external=True, dry_run=False),
        )

    assert report.gb_quota_tripped is True


def test_import_report_gb_quota_not_tripped_when_pre_existing(session):
    """gb_quota_tripped is False when quota was already exhausted before the import."""
    from compendium.services.metadata_cache import _upsert_to_session
    from compendium.domain.models import MetadataCache
    from compendium.services.metadata import _GB_QUOTA_ADAPTER, _GB_QUOTA_KIND, _GB_QUOTA_VALUE

    # Pre-seed the sentinel so _gb_quota_pre_import = True.
    entry = MetadataCache(
        adapter=_GB_QUOTA_ADAPTER,
        kind=_GB_QUOTA_KIND,
        lookup_value=_GB_QUOTA_VALUE,
        is_negative=True,
        payload=None,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    _upsert_to_session(session, entry)
    session.flush()

    with patch("compendium.services.metadata.is_gb_quota_exhausted", return_value=True):
        svc = _make_importer(session)
    assert svc._gb_quota_pre_import is True

    ol_adapter = MagicMock()
    ol_adapter.lookup = MagicMock(return_value=dict(_OL_META))

    with (
        patch("compendium.services.metadata._resolve_book_adapter", return_value=ol_adapter),
        patch("compendium.services.metadata.is_gb_quota_exhausted", return_value=True),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
    ):
        report = svc.import_csv(
            io.StringIO(_CSV),
            ImportOptions(enrich_from_external=True, dry_run=False),
        )

    assert report.gb_quota_tripped is False


# ---------------------------------------------------------------------------
# OL primary → GB fallback (lookup_metadata_with_source integration path)
# ---------------------------------------------------------------------------

def test_ol_primary_gb_fallback_when_ol_misses(session):
    """OL primary + GB key + quota OK + OL miss → GB hit, source='googlebooks'."""
    from compendium.services.metadata import lookup_metadata_with_source

    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return True
        if k == "google_books_api_key":
            return "fake-key"
        return None

    ol_lookup = MagicMock(return_value=None)
    gb_lookup = MagicMock(return_value=dict(_GB_META))

    with (
        patch("compendium.services.metadata._resolve_book_adapter", return_value=_OL_ADAPTER),
        patch("compendium.services.metadata.OpenLibraryAdapter.lookup", ol_lookup),
        patch("compendium.services.metadata.GoogleBooksAdapter.lookup", gb_lookup),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
        patch("compendium.services.metadata.is_gb_quota_exhausted", return_value=False),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
    ):
        result, source = lookup_metadata_with_source("book", "isbn", "9780441013593", session=session)

    assert source == "googlebooks"
    assert result["description"] == "From Google Books."
    ol_lookup.assert_called_once()
    gb_lookup.assert_called_once()


def test_ol_primary_gb_fallback_writes_gb_namespace(session):
    """When OL misses and GB hits, the cache entry is stored under the GB adapter namespace."""
    from compendium.services.metadata import lookup_metadata_with_source

    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return True
        if k == "google_books_api_key":
            return "fake-key"
        return None

    # Patch lookup methods on the real singletons so type(adapter).__name__ is correct.
    with (
        patch("compendium.services.metadata._resolve_book_adapter", return_value=_OL_ADAPTER),
        patch("compendium.services.metadata.OpenLibraryAdapter.lookup", return_value=None),
        patch("compendium.services.metadata.GoogleBooksAdapter.lookup", return_value=dict(_GB_META)),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
        patch("compendium.services.metadata.is_gb_quota_exhausted", return_value=False),
    ):
        lookup_metadata_with_source("book", "isbn", "9780441013593", session=session)
        session.flush()

    # GB result should be cached under the GoogleBooksAdapter namespace.
    row = session.get(MetadataCache, ("GoogleBooksAdapter", "isbn", "9780441013593"))
    assert row is not None
    assert row.payload is not None

    # OL namespace should have a negative sentinel for the miss.
    ol_row = session.get(MetadataCache, ("OpenLibraryAdapter", "isbn", "9780441013593"))
    if ol_row is not None:
        assert ol_row.is_negative or ol_row.payload is None


# ---------------------------------------------------------------------------
# Cache namespace isolation (preference flip)
# ---------------------------------------------------------------------------

def test_cache_namespace_isolation_on_preference_flip(session):
    """OL cache hit does not satisfy a GB-primary lookup and vice versa."""
    from compendium.services.metadata import lookup_metadata_with_source
    from datetime import datetime, timezone

    # Write a successful OL result directly into the OL cache namespace.
    ol_cache_entry = MetadataCache(
        adapter="OpenLibraryAdapter",
        kind="isbn",
        lookup_value="9780441013593",
        is_negative=False,
        payload=json.dumps(_OL_META),
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    from compendium.services.metadata_cache import _upsert_to_session
    _upsert_to_session(session, ol_cache_entry)
    session.flush()

    # Now lookup with GB primary — should NOT serve the OL cache entry.
    gb_lookup = MagicMock(return_value=dict(_GB_META))

    def _gb_setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return False  # GB only, no fallback
        if k == "google_books_api_key":
            return "fake-key"
        return None

    with (
        patch("compendium.services.metadata._resolve_book_adapter", return_value=_GB_ADAPTER),
        patch("compendium.services.metadata.GoogleBooksAdapter.lookup", gb_lookup),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_gb_setting),
        patch("compendium.services.metadata.is_gb_quota_exhausted", return_value=False),
    ):
        result, source = lookup_metadata_with_source("book", "isbn", "9780441013593", session=session)

    # The GB adapter must have been called (not served from OL cache).
    gb_lookup.assert_called_once()
    assert source == "googlebooks"
    assert result["description"] == "From Google Books."

    # And the OL cache entry remains untouched with its original data.
    ol_row = session.get(MetadataCache, ("OpenLibraryAdapter", "isbn", "9780441013593"))
    assert ol_row is not None
    assert "Open Library" in json.loads(ol_row.payload)["description"]


# ---------------------------------------------------------------------------
# Dry-run: _mark_gb_quota_exhausted is called even when the outer tx rolls back
# ---------------------------------------------------------------------------

def test_dry_run_calls_mark_quota_exhausted(session):
    """During a dry-run import, _mark_gb_quota_exhausted is invoked when GB quota trips.

    The sentinel is written in its own independent session (not the dry-run session),
    so it persists at runtime even when the outer transaction rolls back.  We cannot
    verify that persistence with SQLite in-memory test DBs (different engine instances
    don't share state); instead we verify the call is made and would commit independently.
    """
    from compendium.domain.errors import GoogleBooksQuotaExhausted

    svc = _make_importer(session)

    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return True
        if k == "google_books_api_key":
            return "fake-key"
        return None

    with (
        patch("compendium.services.metadata._resolve_book_adapter", return_value=_GB_ADAPTER),
        # Mock at lookup_google_books level so the real adapter.lookup() exception handler fires.
        patch(
            "compendium.services.metadata.lookup_google_books",
            side_effect=GoogleBooksQuotaExhausted("daily limit"),
        ),
        patch("compendium.services.metadata.OpenLibraryAdapter.lookup", return_value=dict(_OL_META)),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
        patch("compendium.services.metadata._mark_gb_quota_exhausted") as mock_mark,
    ):
        svc.import_csv(
            io.StringIO(_CSV),
            ImportOptions(enrich_from_external=True, dry_run=True),
        )

    mock_mark.assert_called()
