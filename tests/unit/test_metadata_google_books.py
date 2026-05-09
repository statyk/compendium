"""Unit tests for the Google Books adapter and quota circuit breaker."""

from __future__ import annotations

from contextvars import copy_context
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from compendium.domain.errors import ExternalLookupError, GoogleBooksQuotaExhausted
from compendium.domain.models import MetadataCache
from compendium.services.metadata import (
    GoogleBooksAdapter,
    _quota_session_factory,
    clear_gb_quota_exhausted,
    is_gb_quota_exhausted,
    lookup_google_books,
    parse_google_books,
)


# ---------------------------------------------------------------------------
# parse_google_books — field mapping
# ---------------------------------------------------------------------------

_VOLUME = {
    "id": "vol_abc123",
    "volumeInfo": {
        "title": "Dune",
        "subtitle": "A Novel",
        "authors": ["Frank Herbert"],
        "publisher": "Chilton Books",
        "publishedDate": "1965-08-01",
        "description": "A sci-fi epic set on a desert planet.",
        "imageLinks": {
            "thumbnail": "http://books.google.com/books/content?id=abc123&zoom=1",
            "small": "http://books.google.com/books/content?id=abc123&zoom=2",
            "medium": "http://books.google.com/books/content?id=abc123&zoom=3",
        },
        "industryIdentifiers": [
            {"type": "ISBN_13", "identifier": "9780441013593"},
        ],
    },
}


def test_parse_google_books_maps_fields():
    result = parse_google_books(_VOLUME, "9780441013593")
    assert result["title"] == "Dune"
    assert result["subtitle"] == "A Novel"
    assert result["authors"] == ["Frank Herbert"]
    assert result["creator_role"] == "author"
    assert result["publisher"] == "Chilton Books"
    assert result["publication_year"] == 1965
    assert result["description"] == "A sci-fi epic set on a desert planet."
    assert result["isbn"] == "9780441013593"
    assert result["upc"] is None
    assert result["external_ids"] == {"google_books": "vol_abc123"}
    assert result["lc_classification"] is None
    assert result["ddc_classification"] is None
    assert result["lccn"] is None


def test_parse_google_books_prefers_larger_image():
    vol = dict(_VOLUME)
    vol["volumeInfo"] = dict(_VOLUME["volumeInfo"])
    vol["volumeInfo"]["imageLinks"] = {
        "thumbnail": "http://books.google.com/thumb",
        "large": "http://books.google.com/large",
    }
    result = parse_google_books(vol, "9780441013593")
    assert result["cover_image_url"] == "https://books.google.com/large"


def test_parse_google_books_upgrades_http_to_https():
    vol = dict(_VOLUME)
    vol["volumeInfo"] = dict(_VOLUME["volumeInfo"])
    vol["volumeInfo"]["imageLinks"] = {"thumbnail": "http://books.google.com/t"}
    result = parse_google_books(vol, "9780441013593")
    assert result["cover_image_url"].startswith("https://")


def test_parse_google_books_no_image():
    vol = dict(_VOLUME)
    vol["volumeInfo"] = dict(_VOLUME["volumeInfo"])
    vol["volumeInfo"]["imageLinks"] = {}
    result = parse_google_books(vol, "9780441013593")
    assert result["cover_image_url"] is None


def test_parse_google_books_partial_date():
    vol = dict(_VOLUME)
    vol["volumeInfo"] = dict(_VOLUME["volumeInfo"])
    vol["volumeInfo"]["publishedDate"] = "1965"
    result = parse_google_books(vol, "9780441013593")
    assert result["publication_year"] == 1965


def test_parse_google_books_no_date():
    vol = dict(_VOLUME)
    vol["volumeInfo"] = dict(_VOLUME["volumeInfo"])
    vol["volumeInfo"]["publishedDate"] = ""
    result = parse_google_books(vol, "9780441013593")
    assert result["publication_year"] is None


# ---------------------------------------------------------------------------
# lookup_google_books — HTTP responses
# ---------------------------------------------------------------------------

def _make_response(status: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = str(body)
    return resp


def _mock_client(resp):
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get = MagicMock(return_value=resp)
    return client


def test_lookup_google_books_hit():
    body = {"items": [_VOLUME]}
    resp = _make_response(200, body)
    with patch("httpx.Client", return_value=_mock_client(resp)):
        result = lookup_google_books("9780441013593", api_key="key")
    assert result == _VOLUME


def test_lookup_google_books_empty_items():
    resp = _make_response(200, {"items": []})
    with patch("httpx.Client", return_value=_mock_client(resp)):
        result = lookup_google_books("9780441013593", api_key="key")
    assert result is None


def test_lookup_google_books_no_items_key():
    resp = _make_response(200, {})
    with patch("httpx.Client", return_value=_mock_client(resp)):
        result = lookup_google_books("9780441013593", api_key="key")
    assert result is None


def test_lookup_google_books_daily_limit_exceeded():
    body = {
        "error": {
            "errors": [{"reason": "dailyLimitExceeded", "domain": "usageLimits"}]
        }
    }
    resp = _make_response(403, body)
    with patch("httpx.Client", return_value=_mock_client(resp)):
        with pytest.raises(GoogleBooksQuotaExhausted):
            lookup_google_books("9780441013593", api_key="key")


def test_lookup_google_books_user_rate_limit_exceeded_also_raises_quota():
    body = {
        "error": {
            "errors": [{"reason": "userRateLimitExceeded", "domain": "usageLimits"}]
        }
    }
    resp = _make_response(403, body)
    with patch("httpx.Client", return_value=_mock_client(resp)):
        with pytest.raises(GoogleBooksQuotaExhausted):
            lookup_google_books("9780441013593", api_key="key")


def test_lookup_google_books_other_403_raises_external_error():
    body = {"error": {"errors": [{"reason": "accessNotConfigured"}]}}
    resp = _make_response(403, body)
    with patch("httpx.Client", return_value=_mock_client(resp)):
        with pytest.raises(ExternalLookupError):
            lookup_google_books("9780441013593", api_key="key")


def test_lookup_google_books_transport_error():
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get = MagicMock(side_effect=httpx.RequestError("timeout"))
    with patch("httpx.Client", return_value=client):
        with pytest.raises(ExternalLookupError):
            lookup_google_books("9780441013593", api_key="key")


def test_lookup_google_books_429_retries_once():
    """HTTP 429 (burst limit) retries once, then raises ExternalLookupError."""
    burst_resp = _make_response(429, {})
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get = MagicMock(return_value=burst_resp)

    with patch("httpx.Client", return_value=client), patch("time.sleep"):
        with pytest.raises(ExternalLookupError):
            lookup_google_books("9780441013593", api_key="key")

    assert client.get.call_count == 2, "should retry exactly once on 429"


def test_lookup_google_books_429_success_on_retry():
    """HTTP 429 on first attempt, 200 on retry."""
    burst_resp = _make_response(429, {})
    ok_resp = _make_response(200, {"items": [_VOLUME]})
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get = MagicMock(side_effect=[burst_resp, ok_resp])

    with patch("httpx.Client", return_value=client), patch("time.sleep"):
        result = lookup_google_books("9780441013593", api_key="key")

    assert result == _VOLUME


# ---------------------------------------------------------------------------
# GoogleBooksAdapter.lookup
# ---------------------------------------------------------------------------

def test_adapter_returns_none_when_no_key():
    adapter = GoogleBooksAdapter()
    with patch("compendium.services.site_settings.get_site_setting", return_value=None):
        result = adapter.lookup("isbn", "9780441013593")
    assert result is None


def test_adapter_raises_on_unsupported_kind():
    adapter = GoogleBooksAdapter()
    with pytest.raises(ExternalLookupError, match="does not support"):
        adapter.lookup("title", "Dune")


def test_adapter_marks_quota_exhausted_on_quota_error():
    adapter = GoogleBooksAdapter()
    with (
        patch("compendium.services.site_settings.get_site_setting", return_value="fake-key"),
        patch("compendium.services.metadata.lookup_google_books", side_effect=GoogleBooksQuotaExhausted()),
        patch("compendium.services.metadata._mark_gb_quota_exhausted") as mock_mark,
    ):
        result = adapter.lookup("isbn", "9780441013593")
    assert result is None
    mock_mark.assert_called_once()


def test_adapter_returns_none_on_miss():
    adapter = GoogleBooksAdapter()
    with (
        patch("compendium.services.site_settings.get_site_setting", return_value="fake-key"),
        patch("compendium.services.metadata.lookup_google_books", return_value=None),
    ):
        result = adapter.lookup("isbn", "9780441013593")
    assert result is None


def test_adapter_returns_parsed_dict_on_hit():
    adapter = GoogleBooksAdapter()
    with (
        patch("compendium.services.site_settings.get_site_setting", return_value="fake-key"),
        patch("compendium.services.metadata.lookup_google_books", return_value=_VOLUME),
    ):
        result = adapter.lookup("isbn", "9780441013593")
    assert result is not None
    assert result["title"] == "Dune"
    assert result["external_ids"] == {"google_books": "vol_abc123"}


# ---------------------------------------------------------------------------
# Library-mode: _quota_session_factory injection
# ---------------------------------------------------------------------------

def _make_sentinel_entry() -> MetadataCache:
    from compendium.services.metadata import _GB_QUOTA_ADAPTER, _GB_QUOTA_KIND, _GB_QUOTA_VALUE

    return MetadataCache(
        adapter=_GB_QUOTA_ADAPTER,
        kind=_GB_QUOTA_KIND,
        lookup_value=_GB_QUOTA_VALUE,
        is_negative=True,
        payload=None,
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def test_is_gb_quota_exhausted_uses_injected_session():
    """is_gb_quota_exhausted reads from an explicit session when provided."""
    mock_session = MagicMock()
    mock_session.get.return_value = None  # no sentinel → not exhausted

    assert is_gb_quota_exhausted(session=mock_session) is False
    assert mock_session.get.called


def test_is_gb_quota_exhausted_returns_true_with_fresh_sentinel_session():
    """Returns True when the provided session has a fresh sentinel row."""
    mock_session = MagicMock()
    mock_session.get.return_value = _make_sentinel_entry()

    assert is_gb_quota_exhausted(session=mock_session) is True


def test_clear_gb_quota_exhausted_uses_injected_session():
    """clear_gb_quota_exhausted deletes from the provided session."""
    mock_session = MagicMock()
    mock_session.get.return_value = _make_sentinel_entry()

    result = clear_gb_quota_exhausted(session=mock_session)

    assert result is True
    mock_session.delete.assert_called_once()


def test_clear_gb_quota_exhausted_returns_false_when_no_sentinel():
    mock_session = MagicMock()
    mock_session.get.return_value = None

    result = clear_gb_quota_exhausted(session=mock_session)

    assert result is False
    mock_session.delete.assert_not_called()


def test_quota_session_factory_injected_for_is_exhausted():
    """_quota_session_factory is consulted when no explicit session is given."""
    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = None

    factory = MagicMock(return_value=mock_session)

    def _run():
        _quota_session_factory.set(factory)
        return is_gb_quota_exhausted()

    ctx = copy_context()
    result = ctx.run(_run)

    assert result is False
    factory.assert_called_once()


def test_quota_session_factory_injected_for_mark_exhausted():
    """_mark_gb_quota_exhausted writes to the injected factory's session."""
    from compendium.services.metadata import _mark_gb_quota_exhausted

    mock_session = MagicMock()
    mock_session.__enter__ = lambda s: s
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.get.return_value = None

    factory = MagicMock(return_value=mock_session)

    def _run():
        _quota_session_factory.set(factory)
        _mark_gb_quota_exhausted()

    ctx = copy_context()
    ctx.run(_run)

    factory.assert_called_once()
    mock_session.add.assert_called_once()
