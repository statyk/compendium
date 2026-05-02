"""Unit tests for cover-image fallback and classification lookups."""

from unittest.mock import MagicMock, patch

import pytest

from compendium.services.metadata import (
    lookup_cover_fallbacks,
    lookup_cover_from_google_books,
    pick_classification_code,
)

# ---------------------------------------------------------------------------
# lookup_cover_from_google_books
# ---------------------------------------------------------------------------

_GOOGLE_BOOKS_RESPONSE = {
    "items": [{
        "volumeInfo": {
            "imageLinks": {
                "thumbnail": "http://books.google.com/books/content?id=X&zoom=1",
                "small": "http://books.google.com/books/content?id=X&zoom=2",
            }
        }
    }]
}


def test_google_books_cover_returns_none_without_key():
    assert lookup_cover_from_google_books("9780441013593", api_key=None) is None


def test_google_books_cover_returns_none_without_isbn():
    assert lookup_cover_from_google_books("", api_key="key") is None


def test_google_books_cover_parses_thumbnail():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _GOOGLE_BOOKS_RESPONSE

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        result = lookup_cover_from_google_books("9780441013593", api_key="key")

    assert "books.google.com" in result


def test_google_books_cover_returns_none_when_no_items():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"items": []}

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        result = lookup_cover_from_google_books("9780441013593", api_key="key")

    assert result is None


def test_google_books_cover_returns_none_on_non_200():
    mock_resp = MagicMock()
    mock_resp.status_code = 403

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        result = lookup_cover_from_google_books("9780441013593", api_key="key")

    assert result is None


def test_google_books_cover_returns_none_on_network_error():
    import httpx

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("x")
        result = lookup_cover_from_google_books("9780441013593", api_key="key")

    assert result is None


# ---------------------------------------------------------------------------
# lookup_cover_fallbacks — delegates to Google Books
# ---------------------------------------------------------------------------

def test_fallbacks_returns_google_books_url():
    with patch("compendium.services.metadata.lookup_cover_from_google_books",
               return_value="http://books.google.com/cover") as mock_gb:
        result = lookup_cover_fallbacks("9780441013593", google_books_key="gb")

    assert result == "http://books.google.com/cover"
    mock_gb.assert_called_once_with("9780441013593", api_key="gb")


def test_fallbacks_returns_none_when_google_books_fails():
    with patch("compendium.services.metadata.lookup_cover_from_google_books", return_value=None):
        result = lookup_cover_fallbacks("9780441013593", google_books_key=None)

    assert result is None


# ---------------------------------------------------------------------------
# pick_classification_code
# ---------------------------------------------------------------------------

def test_pick_classification_code_lcc_uses_meta_value():
    result = pick_classification_code("lcc", {"lc_classification": "PS3537.A618"})
    assert result == "PS3537.A618"


def test_pick_classification_code_ddc_uses_meta_value():
    result = pick_classification_code("ddc", {"ddc_classification": "823.914"})
    assert result == "823.914"


def test_pick_classification_code_none_returns_none():
    result = pick_classification_code("none", {"lc_classification": "PS3537"})
    assert result is None


def test_pick_classification_code_unknown_scheme_returns_none():
    result = pick_classification_code("udc", {"lc_classification": "PS3537"})
    assert result is None
