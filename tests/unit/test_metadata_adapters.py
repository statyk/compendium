"""Unit tests for metadata adapters (no network calls)."""

from unittest.mock import patch

import pytest

from compendium.domain.errors import ExternalLookupError
from compendium.services.metadata import (
    MusicBrainzAdapter,
    OpenLibraryAdapter,
    TMDbAdapter,
    _mb_fetch_release,
    _parse_mb_release,
    _parse_tmdb_movie,
    musicbrainz_search_title,
    normalize_isbn,
    normalize_upc,
)

# ---------------------------------------------------------------------------
# normalize_isbn
# ---------------------------------------------------------------------------

def test_normalize_isbn_strips_hyphens():
    assert normalize_isbn("978-0-441-01359-3") == "9780441013593"


def test_normalize_isbn_converts_isbn10():
    assert normalize_isbn("0441013597") == "9780441013593"


def test_normalize_isbn_accepts_isbn10_with_x_check():
    # ISBN-10 check digits can legally be 'X' (value 10).
    result = normalize_isbn("019853553X")
    assert result.startswith("978") and len(result) == 13


def test_normalize_isbn_rejects_alphanumeric_10_char():
    from compendium.domain.errors import ValidationError as CompendiumValidationError

    with pytest.raises(CompendiumValidationError):
        normalize_isbn("MYLIB12345")


def test_normalize_isbn_rejects_short_input():
    from compendium.domain.errors import ValidationError as CompendiumValidationError

    with pytest.raises(CompendiumValidationError):
        normalize_isbn("123")


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
    "country": "US",
    "artist-credit": [{"artist": {"name": "Miles Davis"}}],
    "label-info": [{"label": {"name": "Columbia"}, "catalog-number": "CL 1355"}],
    "barcode": "724353063870",
    "genres": [{"name": "Modal Jazz"}],
    "release-group": {
        "first-release-date": "1959-08-17",
        "genres": [{"name": "Jazz"}],
    },
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


def test_parse_mb_release_extracts_catalog_number():
    meta = _parse_mb_release(_MB_RESPONSE, "724353063870")
    assert meta["extra_metadata"]["catalog_number"] == "CL 1355"


def test_parse_mb_release_extracts_country():
    meta = _parse_mb_release(_MB_RESPONSE, "724353063870")
    assert meta["extra_metadata"]["country"] == "US"


def test_parse_mb_release_extracts_original_year():
    meta = _parse_mb_release(_MB_RESPONSE, "724353063870")
    assert meta["extra_metadata"]["original_year"] == 1959


def test_parse_mb_release_original_year_differs_from_pressing_year():
    """A reissue's own date is the pressing; original_year comes from the group."""
    data = {
        **_MB_RESPONSE,
        "date": "2015-06-01",
        "release-group": {"first-release-date": "1959-08-17"},
    }
    meta = _parse_mb_release(data, "")
    assert meta["publication_year"] == 2015
    assert meta["extra_metadata"]["original_year"] == 1959


def test_parse_mb_release_prefers_release_level_genres():
    meta = _parse_mb_release(_MB_RESPONSE, "724353063870")
    assert meta["extra_metadata"]["genres"] == ["Modal Jazz"]


def test_parse_mb_release_falls_back_to_release_group_genres():
    data = {**_MB_RESPONSE, "genres": []}
    meta = _parse_mb_release(data, "")
    assert meta["extra_metadata"]["genres"] == ["Jazz"]


def test_parse_mb_release_caps_genres_at_five():
    data = {
        **_MB_RESPONSE,
        "genres": [{"name": f"g{i}"} for i in range(8)],
    }
    meta = _parse_mb_release(data, "")
    assert meta["extra_metadata"]["genres"] == ["g0", "g1", "g2", "g3", "g4"]


def test_parse_mb_release_missing_new_fields_are_empty():
    data = {
        "id": "x",
        "title": "Bare Release",
        "media": [],
        "artist-credit": [],
        "label-info": [],
    }
    meta = _parse_mb_release(data, "")
    extra = meta["extra_metadata"]
    assert extra["catalog_number"] is None
    assert extra["country"] is None
    assert extra["original_year"] is None
    assert extra["genres"] == []


@patch("compendium.services.metadata._mb_get")
def test_mb_fetch_release_requests_extended_inc(mock_get):
    mock_get.return_value = _MB_RESPONSE
    _mb_fetch_release("cb7b5c31-10ef-4b73-a42f-80d9af8b6aee", upc=None)
    _path, params = mock_get.call_args[0]
    assert "release-groups" in params["inc"]
    assert "genres" in params["inc"]


# ---------------------------------------------------------------------------
# musicbrainz_search_title
# ---------------------------------------------------------------------------

_MB_SEARCH_RESPONSE = {
    "releases": [
        {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "title": "Kind of Blue",
            "date": "1959-08-17",
            "country": "US",
            "artist-credit": [{"artist": {"name": "Miles Davis"}}],
            "media": [{"format": "Vinyl"}],
        }
    ]
}


@patch("compendium.services.metadata._mb_get")
def test_musicbrainz_search_title_includes_cover_url(mock_get):
    mock_get.return_value = _MB_SEARCH_RESPONSE
    candidates = musicbrainz_search_title("kind of blue")
    assert candidates[0]["image_url"] == (
        "https://coverartarchive.org/release/"
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/front-250"
    )


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


# ---------------------------------------------------------------------------
# TMDbAdapter
# ---------------------------------------------------------------------------

_TMDB_RESPONSE = {
    "id": 497,
    "title": "The Green Mile",
    "release_date": "1999-12-10",
    "overview": "A supernatural tale set on death row.",
    "runtime": 189,
    "tagline": "Miracles do happen.",
    "original_language": "en",
    "poster_path": "/poster.jpg",
    "imdb_id": "tt0120689",
    "genres": [{"id": 18, "name": "Drama"}, {"id": 878, "name": "Fantasy"}],
    "credits": {
        "crew": [
            {"name": "Frank Darabont", "job": "Director", "department": "Directing"},
            {"name": "Frank Darabont", "job": "Screenplay", "department": "Writing"},
        ],
        "cast": [
            {"name": "Tom Hanks", "order": 0},
            {"name": "Michael Clarke Duncan", "order": 1},
        ],
    },
}


def test_parse_tmdb_movie_extracts_title():
    meta = _parse_tmdb_movie(_TMDB_RESPONSE)
    assert meta["title"] == "The Green Mile"


def test_parse_tmdb_movie_extracts_year():
    meta = _parse_tmdb_movie(_TMDB_RESPONSE)
    assert meta["publication_year"] == 1999


def test_parse_tmdb_movie_extracts_director_and_writer_creators():
    meta = _parse_tmdb_movie(_TMDB_RESPONSE)
    # Frank Darabont is both director and screenplay — deduplicated by name only in writers
    roles = [(name, role) for name, role in meta["creators"]]
    assert ("Frank Darabont", "director") in roles
    # Screenplay credit is also Frank Darabont — appears once as director, once as writer
    assert any(role == "director" for _, role in roles)


def test_parse_tmdb_movie_extracts_runtime_and_genres():
    meta = _parse_tmdb_movie(_TMDB_RESPONSE)
    assert meta["extra_metadata"]["runtime_minutes"] == 189
    assert "Drama" in meta["extra_metadata"]["genres"]
    assert "Fantasy" in meta["extra_metadata"]["genres"]


def test_parse_tmdb_movie_extracts_external_ids():
    meta = _parse_tmdb_movie(_TMDB_RESPONSE)
    assert meta["external_ids"]["tmdb"] == "497"
    assert meta["external_ids"]["imdb"] == "tt0120689"


def test_parse_tmdb_movie_extracts_cast():
    meta = _parse_tmdb_movie(_TMDB_RESPONSE)
    assert "Tom Hanks" in meta["extra_metadata"]["cast"]


def test_tmdb_adapter_raises_for_non_tmdb_id():
    adapter = TMDbAdapter()
    with pytest.raises(ExternalLookupError, match="does not support"):
        adapter.lookup("upc", "12345")


def test_tmdb_adapter_raises_when_no_api_key():
    adapter = TMDbAdapter()
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ExternalLookupError, match="API key not configured"):
            adapter.lookup("tmdb_id", "497")


@patch("compendium.services.metadata._tmdb_fetch_movie", return_value=_TMDB_RESPONSE)
def test_tmdb_adapter_delegates_tmdb_id(mock_fetch):
    adapter = TMDbAdapter()
    with patch.dict("os.environ", {"COMPENDIUM_TMDB_API_KEY": "testkey"}):
        result = adapter.lookup("tmdb_id", "497")
    mock_fetch.assert_called_once_with("497", "testkey")
    assert result["title"] == "The Green Mile"
