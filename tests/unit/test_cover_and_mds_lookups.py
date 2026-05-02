"""Unit tests for cover-image fallback and MDS classification lookups."""

from unittest.mock import MagicMock, patch

import pytest

from compendium.services.metadata import (
    lookup_cover_fallbacks,
    lookup_cover_from_google_books,
    lookup_cover_from_librarything,
    lookup_mds_from_librarything,
    pick_classification_code,
)

# ---------------------------------------------------------------------------
# lookup_mds_from_librarything
# ---------------------------------------------------------------------------

_MDS_XML_HIT = """<?xml version="1.0"?>
<response>
  <ltml>
    <item>
      <commonknowledge>
        <fieldList>
          <field type="mds" name="Melvil Decimal System (MDS)">
            <factlist>
              <fact>823.914</fact>
            </factlist>
          </field>
        </fieldList>
      </commonknowledge>
    </item>
  </ltml>
</response>"""

_MDS_XML_MISS = """<?xml version="1.0"?>
<response><ltml><item><commonknowledge><fieldList/></commonknowledge></item></ltml></response>"""


def test_mds_returns_none_when_no_api_key():
    result = lookup_mds_from_librarything("9780441013593", api_key=None)
    assert result is None


def test_mds_returns_none_when_empty_isbn():
    result = lookup_mds_from_librarything("", api_key="testkey")
    assert result is None


def test_mds_parses_code_from_response():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = _MDS_XML_HIT

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        result = lookup_mds_from_librarything("9780441013593", api_key="testkey")

    assert result == "823.914"


def test_mds_returns_none_when_field_missing():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = _MDS_XML_MISS

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        result = lookup_mds_from_librarything("9780441013593", api_key="testkey")

    assert result is None


def test_mds_returns_none_on_non_200():
    mock_resp = MagicMock()
    mock_resp.status_code = 500

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        result = lookup_mds_from_librarything("9780441013593", api_key="testkey")

    assert result is None


def test_mds_returns_none_on_malformed_xml():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "this is not xml <<<"

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        result = lookup_mds_from_librarything("9780441013593", api_key="testkey")

    assert result is None


def test_mds_returns_none_on_network_error():
    import httpx

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("x")
        result = lookup_mds_from_librarything("9780441013593", api_key="testkey")

    assert result is None


# ---------------------------------------------------------------------------
# pick_classification_code — mds branch
# ---------------------------------------------------------------------------

def test_pick_classification_code_mds_delegates_to_librarything():
    with patch("compendium.services.metadata.lookup_mds_from_librarything", return_value="823.914") as mock_lt:
        result = pick_classification_code("mds", {"isbn": "9780441013593"}, librarything_api_key="key")
    assert result == "823.914"
    mock_lt.assert_called_once_with("9780441013593", api_key="key")


def test_pick_classification_code_mds_returns_none_without_key():
    with patch("compendium.services.metadata.lookup_mds_from_librarything", return_value=None) as mock_lt:
        result = pick_classification_code("mds", {"isbn": "9780441013593"}, librarything_api_key=None)
    assert result is None


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

    # small takes precedence over thumbnail in image priority
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
# lookup_cover_from_librarything
# ---------------------------------------------------------------------------

def test_lt_cover_returns_none_without_key():
    assert lookup_cover_from_librarything("9780441013593", api_key=None) is None


def test_lt_cover_returns_none_without_isbn():
    assert lookup_cover_from_librarything("", api_key="key") is None


def test_lt_cover_returns_url_when_real_image():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "image/jpeg", "content-length": "24000"}

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.head.return_value = mock_resp
        result = lookup_cover_from_librarything("9780441013593", api_key="testkey")

    assert result is not None
    assert "covers.librarything.com" in result
    assert "testkey" in result


def test_lt_cover_returns_none_for_placeholder_image():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "image/gif", "content-length": "43"}

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.head.return_value = mock_resp
        result = lookup_cover_from_librarything("9780441013593", api_key="testkey")

    assert result is None


def test_lt_cover_returns_none_on_network_error():
    import httpx

    with patch("compendium.services.metadata.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.head.side_effect = httpx.ConnectError("x")
        result = lookup_cover_from_librarything("9780441013593", api_key="testkey")

    assert result is None


# ---------------------------------------------------------------------------
# lookup_cover_fallbacks — orchestration
# ---------------------------------------------------------------------------

def test_fallbacks_tries_google_books_first():
    with patch("compendium.services.metadata.lookup_cover_from_google_books", return_value="http://books.google.com/cover") as mock_gb, \
         patch("compendium.services.metadata.lookup_cover_from_librarything") as mock_lt:
        result = lookup_cover_fallbacks("9780441013593", google_books_key="gb", librarything_key="lt")

    assert result == "http://books.google.com/cover"
    mock_lt.assert_not_called()


def test_fallbacks_tries_librarything_when_google_books_fails():
    with patch("compendium.services.metadata.lookup_cover_from_google_books", return_value=None), \
         patch("compendium.services.metadata.lookup_cover_from_librarything", return_value="https://covers.librarything.com/devkey/lt/large/isbn/X") as mock_lt:
        result = lookup_cover_fallbacks("9780441013593", google_books_key="gb", librarything_key="lt")

    assert "librarything.com" in result


def test_fallbacks_returns_none_when_both_fail():
    with patch("compendium.services.metadata.lookup_cover_from_google_books", return_value=None), \
         patch("compendium.services.metadata.lookup_cover_from_librarything", return_value=None):
        result = lookup_cover_fallbacks("9780441013593", google_books_key=None, librarything_key=None)

    assert result is None
