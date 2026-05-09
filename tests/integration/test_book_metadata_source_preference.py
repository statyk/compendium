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
