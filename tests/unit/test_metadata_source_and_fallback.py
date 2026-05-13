"""Unit tests for _resolve_book_chain, lookup_metadata_with_source, and GB↔OL fallback."""

from unittest.mock import patch, MagicMock

import pytest

from compendium.domain.errors import ExternalLookupError
from compendium.services.metadata import (
    _GB_ADAPTER,
    _OL_ADAPTER,
    _resolve_book_chain,
    lookup_metadata,
    lookup_metadata_with_source,
)

_BOOK_RESULT = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert", "role": "author"}],
    "cover_image_url": None,
    "external_ids": {"isbn": "9780441172719"},
}

_OL_RESULT = {
    "title": "Dune (OL)",
    "authors": [{"name": "Frank Herbert", "role": "author"}],
    "cover_image_url": None,
    "external_ids": {"isbn": "9780441172719"},
}


def _mock_gb(return_value):
    return patch(
        "compendium.services.metadata.GoogleBooksAdapter.lookup",
        return_value=return_value,
    )


def _mock_gb_raise(exc):
    return patch(
        "compendium.services.metadata.GoogleBooksAdapter.lookup",
        side_effect=exc,
    )


def _mock_ol(return_value):
    return patch(
        "compendium.services.metadata.OpenLibraryAdapter.lookup",
        return_value=return_value,
    )


def _mock_ol_raise(exc):
    return patch(
        "compendium.services.metadata.OpenLibraryAdapter.lookup",
        side_effect=exc,
    )


def _mock_gb_primary():
    """Make _resolve_book_adapter return the real _GB_ADAPTER instance."""
    from compendium.services.metadata import _GB_ADAPTER
    return patch("compendium.services.metadata._resolve_book_adapter", return_value=_GB_ADAPTER)


def _mock_ol_primary():
    """Make _resolve_book_adapter return the real _OL_ADAPTER instance."""
    from compendium.services.metadata import _OL_ADAPTER
    return patch("compendium.services.metadata._resolve_book_adapter", return_value=_OL_ADAPTER)


# ---------------------------------------------------------------------------
# lookup_metadata_with_source — GB primary, success
# ---------------------------------------------------------------------------

def test_gb_primary_hit_returns_googlebooks_source():
    with _mock_gb_primary(), _mock_gb(_BOOK_RESULT):
        result, source = lookup_metadata_with_source("book", "isbn", "9780441172719")
    assert source == "googlebooks"
    assert result["title"] == "Dune"


# ---------------------------------------------------------------------------
# lookup_metadata_with_source — GB miss, OL fallback succeeds
# ---------------------------------------------------------------------------

def test_gb_primary_miss_falls_back_to_ol():
    with _mock_gb_primary(), _mock_gb(None), _mock_ol(_OL_RESULT):
        result, source = lookup_metadata_with_source("book", "isbn", "9780441172719")
    assert source == "openlibrary"
    assert result["title"] == "Dune (OL)"


# ---------------------------------------------------------------------------
# lookup_metadata_with_source — GB raises ExternalLookupError, OL fallback succeeds
# ---------------------------------------------------------------------------

def test_gb_error_falls_back_to_ol():
    err = ExternalLookupError("Google Books returned HTTP 400")
    with _mock_gb_primary(), _mock_gb_raise(err), _mock_ol(_OL_RESULT):
        result, source = lookup_metadata_with_source("book", "isbn", "9780441172719")
    assert source == "openlibrary"
    assert result["title"] == "Dune (OL)"


# ---------------------------------------------------------------------------
# lookup_metadata_with_source — GB swallowed but OL also errors → propagates
# ---------------------------------------------------------------------------

def test_gb_error_ol_also_fails_raises():
    """GB error is swallowed; when OL (secondary) also fails, its error propagates."""
    err = ExternalLookupError("Google Books returned HTTP 400")
    ol_err = ExternalLookupError("Open Library unavailable")
    with _mock_gb_primary(), _mock_gb_raise(err), _mock_ol_raise(ol_err):
        with pytest.raises(ExternalLookupError, match="Open Library unavailable"):
            lookup_metadata_with_source("book", "isbn", "9780441172719")


# ---------------------------------------------------------------------------
# lookup_metadata_with_source — OL primary, success
# ---------------------------------------------------------------------------

def test_ol_primary_hit_returns_openlibrary_source():
    with _mock_ol_primary(), _mock_ol(_OL_RESULT):
        result, source = lookup_metadata_with_source("book", "isbn", "9780441172719")
    assert source == "openlibrary"
    assert result["title"] == "Dune (OL)"


# ---------------------------------------------------------------------------
# lookup_metadata_with_source — OL primary raises → exception propagates (no fallback)
# ---------------------------------------------------------------------------

def test_ol_primary_error_propagates():
    err = ExternalLookupError("Open Library returned HTTP 503")
    with _mock_ol_primary(), _mock_ol_raise(err):
        with pytest.raises(ExternalLookupError):
            lookup_metadata_with_source("book", "isbn", "9780441172719")


# ---------------------------------------------------------------------------
# Back-compat: lookup_metadata returns the dict only
# ---------------------------------------------------------------------------

def test_lookup_metadata_back_compat():
    with _mock_gb_primary(), _mock_gb(_BOOK_RESULT):
        result = lookup_metadata("book", "isbn", "9780441172719")
    assert isinstance(result, dict)
    assert result["title"] == "Dune"


def test_lookup_metadata_back_compat_returns_none_on_total_miss():
    with _mock_gb_primary(), _mock_gb(None), _mock_ol(None):
        result = lookup_metadata("book", "isbn", "9780441172719")
    assert result is None


# ---------------------------------------------------------------------------
# _resolve_book_chain — full matrix
# ---------------------------------------------------------------------------

def _patch_fallback(enabled: bool):
    """Patch book_metadata_fallback_enabled site setting."""
    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return enabled
        return None
    return patch(
        "compendium.services.site_settings.get_site_setting",
        side_effect=_setting,
    )


def _patch_gb_key(key_value):
    """Patch google_books_api_key site setting."""
    def _setting(k, **kw):
        if k == "google_books_api_key":
            return key_value
        if k == "book_metadata_fallback_enabled":
            return True
        return None
    return patch(
        "compendium.services.site_settings.get_site_setting",
        side_effect=_setting,
    )


def _patch_quota(exhausted: bool):
    return patch("compendium.services.metadata.is_gb_quota_exhausted", return_value=exhausted)


def test_chain_gb_primary_fallback_on():
    """GB primary + fallback on → [GB, OL]."""
    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return True
        return None
    with (
        _mock_gb_primary(),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
    ):
        chain = _resolve_book_chain()
    assert chain == [_GB_ADAPTER, _OL_ADAPTER]


def test_chain_gb_primary_fallback_off():
    """GB primary + fallback off → [GB] only."""
    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return False
        return None
    with (
        _mock_gb_primary(),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
    ):
        chain = _resolve_book_chain()
    assert chain == [_GB_ADAPTER]


def test_chain_ol_primary_fallback_off():
    """OL primary + fallback off → [OL] regardless of key/quota."""
    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return False
        if k == "google_books_api_key":
            return "fake-key"
        return None
    with (
        _mock_ol_primary(),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
        _patch_quota(False),
    ):
        chain = _resolve_book_chain()
    assert chain == [_OL_ADAPTER]


def test_chain_ol_primary_fallback_on_no_gb_key():
    """OL primary + fallback on + no GB key → [OL] only."""
    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return True
        if k == "google_books_api_key":
            return None
        return None
    with (
        _mock_ol_primary(),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
    ):
        chain = _resolve_book_chain()
    assert chain == [_OL_ADAPTER]


def test_chain_ol_primary_fallback_on_gb_key_quota_ok():
    """OL primary + fallback on + GB key + quota OK → [OL, GB]."""
    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return True
        if k == "google_books_api_key":
            return "fake-key"
        return None
    with (
        _mock_ol_primary(),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
        _patch_quota(False),
    ):
        chain = _resolve_book_chain()
    assert chain == [_OL_ADAPTER, _GB_ADAPTER]


def test_chain_ol_primary_fallback_on_gb_key_quota_tripped():
    """OL primary + fallback on + GB key set + quota tripped → [OL] only."""
    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return True
        if k == "google_books_api_key":
            return "fake-key"
        return None
    with (
        _mock_ol_primary(),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
        _patch_quota(True),
    ):
        chain = _resolve_book_chain()
    assert chain == [_OL_ADAPTER]


# ---------------------------------------------------------------------------
# OL primary → GB fallback end-to-end (lookup_metadata_with_source)
# ---------------------------------------------------------------------------

def test_ol_primary_miss_falls_back_to_gb():
    """OL primary + GB key + quota OK + OL miss → GB hit, source = googlebooks."""
    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return True
        if k == "google_books_api_key":
            return "fake-key"
        return None

    with (
        _mock_ol_primary(),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
        _patch_quota(False),
        _mock_ol(None),
        _mock_gb(_BOOK_RESULT),
    ):
        result, source = lookup_metadata_with_source("book", "isbn", "9780441172719")

    assert source == "googlebooks"
    assert result["title"] == "Dune"


def test_ol_primary_fallback_off_ol_miss_returns_none():
    """OL primary + fallback off + OL miss → no GB attempt, returns None."""
    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return False
        if k == "google_books_api_key":
            return "fake-key"
        return None
    gb_mock = MagicMock(return_value=_BOOK_RESULT)

    with (
        _mock_ol_primary(),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
        _patch_quota(False),
        _mock_ol(None),
        patch("compendium.services.metadata.GoogleBooksAdapter.lookup", gb_mock),
    ):
        result, source = lookup_metadata_with_source("book", "isbn", "9780441172719")

    assert result is None
    assert source is None
    gb_mock.assert_not_called()


def test_gb_primary_transport_error_ol_fallback_tried():
    """GB primary + transport ExternalLookupError → OL secondary still tried."""
    def _setting(k, **kw):
        if k == "book_metadata_fallback_enabled":
            return True
        return None
    err = ExternalLookupError("GB transport error")

    with (
        _mock_gb_primary(),
        patch("compendium.services.site_settings.get_site_setting", side_effect=_setting),
        _mock_gb_raise(err),
        _mock_ol(_OL_RESULT),
    ):
        result, source = lookup_metadata_with_source("book", "isbn", "9780441172719")

    assert source == "openlibrary"
    assert result["title"] == "Dune (OL)"
