"""Unit tests for metadata adapters (no network calls)."""

from unittest.mock import patch

import pytest

from compendium.domain.errors import ExternalLookupError
from compendium.services.metadata import (
    MusicBrainzAdapter,
    OpenLibraryAdapter,
    _parse_mb_release,
    normalize_upc,
)

# ---------------------------------------------------------------------------
# normalize_upc
# ---------------------------------------------------------------------------

def test_normalize_upc_strips_spaces():
    assert normalize_upc("724353 063870") == "724353063870"


def test_normalize_upc_strips_hyphens():
    assert normalize_upc("724353-063870") == "724353063870"


def test_normalize_upc_rejects_letters():
    with pytest.raises(Exception):
        normalize_upc("ABC123")


def test_normalize_upc_rejects_wrong_length():
    with pytest.raises(Exception):
        normalize_upc("123")


# ---------------------------------------------------------------------------
# _parse_mb_release
# ---------------------------------------------------------------------------

_MB_RESPONSE = {
    "id": "cb7b5c31-10ef-4b73-a42f-80d9af8b6aee",
    "title": "Kind of Blue",
    "date": "1959-08-17",
    "artist-credit": [{"artist": {"name": "Miles Davis"}}],
    "label-info": [{"label": {"name": "Columbia"}}],
    "barcode": "724353063870",
    "media": [
        {
            "format": "Vinyl",
            "tracks": [
                {"position": 1, "title": "So What", "length": 562000},
                {"position": 2, "title": "Freddie Freeloader", "length": 583000},
            ],
        }
    ],
}


def test_parse_mb_release_extracts_title():
    meta = _parse_mb_release(_MB_RESPONSE, "724353063870")
    assert meta["title"] == "Kind of Blue"


def test_parse_mb_release_extracts_artist():
    meta = _parse_mb_release(_MB_RESPONSE, "724353063870")
    assert meta["authors"] == ["Miles Davis"]
    assert meta["creator_role"] == "artist"


def test_parse_mb_release_extracts_year():
    meta = _parse_mb_release(_MB_RESPONSE, "724353063870")
    assert meta["publication_year"] == 1959


def test_parse_mb_release_extracts_label():
    meta = _parse_mb_release(_MB_RESPONSE, "724353063870")
    assert meta["publisher"] == "Columbia"


def test_parse_mb_release_extracts_tracks():
    meta = _parse_mb_release(_MB_RESPONSE, "724353063870")
    assert meta["extra_metadata"]["track_count"] == 2
    assert meta["extra_metadata"]["tracks"][0]["title"] == "So What"
    assert meta["extra_metadata"]["tracks"][0]["length_ms"] == 562000


def test_parse_mb_release_stores_upc():
    meta = _parse_mb_release(_MB_RESPONSE, "724353063870")
    assert meta["upc"] == "724353063870"


def test_parse_mb_release_stores_mbid():
    meta = _parse_mb_release(_MB_RESPONSE, "724353063870")
    assert meta["external_ids"]["musicbrainz"] == "cb7b5c31-10ef-4b73-a42f-80d9af8b6aee"


def test_parse_mb_release_missing_label_is_none():
    data = {**_MB_RESPONSE, "label-info": []}
    meta = _parse_mb_release(data, "")
    assert meta["publisher"] is None


# ---------------------------------------------------------------------------
# MusicBrainzAdapter
# ---------------------------------------------------------------------------

def test_musicbrainz_adapter_raises_for_unknown_kind():
    adapter = MusicBrainzAdapter()
    with pytest.raises(ExternalLookupError, match="does not support"):
        adapter.lookup("isbn", "123")


@patch("compendium.services.metadata._mb_lookup_by_upc")
def test_musicbrainz_adapter_delegates_upc(mock_lookup):
    mock_lookup.return_value = {"title": "Test"}
    adapter = MusicBrainzAdapter()
    result = adapter.lookup("upc", "724353063870")
    mock_lookup.assert_called_once_with("724353063870")
    assert result == {"title": "Test"}


@patch("compendium.services.metadata._mb_lookup_by_mbid")
def test_musicbrainz_adapter_delegates_mbid(mock_lookup):
    mbid = "cb7b5c31-10ef-4b73-a42f-80d9af8b6aee"
    mock_lookup.return_value = {"title": "Test"}
    adapter = MusicBrainzAdapter()
    result = adapter.lookup("mbid", mbid)
    mock_lookup.assert_called_once_with(mbid)
    assert result == {"title": "Test"}


# ---------------------------------------------------------------------------
# OpenLibraryAdapter
# ---------------------------------------------------------------------------

def test_open_library_adapter_raises_for_non_isbn():
    adapter = OpenLibraryAdapter()
    with pytest.raises(ExternalLookupError, match="does not support"):
        adapter.lookup("upc", "12345")


@patch("compendium.services.metadata.lookup_isbn", return_value={})
def test_open_library_adapter_returns_none_when_not_found(_):
    adapter = OpenLibraryAdapter()
    result = adapter.lookup("isbn", "9780441013593")
    assert result is None
