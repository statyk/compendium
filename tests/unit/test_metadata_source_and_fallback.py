"""Unit tests for lookup_metadata_with_source and GB→OL fallback on errors."""

from unittest.mock import patch, MagicMock

import pytest

from compendium.domain.errors import ExternalLookupError
from compendium.services.metadata import lookup_metadata, lookup_metadata_with_source

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
# lookup_metadata_with_source — both GB and OL fail → (None, None)
# ---------------------------------------------------------------------------

def test_gb_error_ol_also_fails_returns_none():
    err = ExternalLookupError("Google Books returned HTTP 400")
    ol_err = ExternalLookupError("Open Library unavailable")
    with _mock_gb_primary(), _mock_gb_raise(err), _mock_ol_raise(ol_err):
        result, source = lookup_metadata_with_source("book", "isbn", "9780441172719")
    assert result is None
    assert source is None


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
