"""Unit tests for the Discogs CSV importer translation layer."""
from __future__ import annotations

import pytest

from compendium.domain.errors import ValidationError
from compendium.services.import_export import (
    _discogs_grade,
    _discogs_media_type,
    _discogs_to_compendium,
)


@pytest.mark.parametrize("fmt,expected", [
    ("Vinyl, LP, Album, Reissue", "vinyl"),
    ("2xVinyl, LP, Compilation", "vinyl"),
    ('12", 45 RPM', "vinyl"),
    ("CD, Album", "cd"),
    ("CD, Album, Reissue", "cd"),
    ("Cassette, Album", None),
    ("File, FLAC, Album", None),
    ("", None),
])
def test_discogs_media_type(fmt, expected):
    assert _discogs_media_type(fmt) == expected


@pytest.mark.parametrize("value,expected", [
    ("Near Mint (NM or M-)", "NM"),
    ("Very Good Plus (VG+)", "VG+"),
    ("Mint (M)", "M"),
    ("Generic", "Gen"),
    ("No Cover", "NoCvr"),
    ("Not Graded", None),
    ("", None),
    (None, None),
])
def test_discogs_grade_known(value, expected):
    assert _discogs_grade(value) == expected


def test_discogs_grade_unknown_passes_through_truncated():
    assert _discogs_grade("Some Weird Long Grade Name") == "Some Weird Long "  # 16 chars


def _discogs_row(**overrides):
    base = {
        "Catalog#": "CL 1355",
        "Artist": "Miles Davis",
        "Title": "Kind of Blue",
        "Label": "Columbia",
        "Format": "Vinyl, LP, Album, Reissue",
        "Rating": "5",
        "Released": "1959",
        "release_id": "12345",
        "CollectionFolder": "Jazz Shelf",
        "Date Added": "2026-01-02 10:00:00",
        "Collection Media Condition": "Near Mint (NM or M-)",
        "Collection Sleeve Condition": "Very Good Plus (VG+)",
        "Collection Notes": "first pressing",
    }
    base.update(overrides)
    return base


def test_discogs_happy_row():
    row, copies = _discogs_to_compendium(_discogs_row())
    assert copies == 1
    assert row["title"] == "Kind of Blue"
    assert row["_creators"] == [("Miles Davis", "artist")]
    assert row["publisher"] == "Columbia"
    assert row["publication_year"] == "1959"
    assert row["media_type"] == "vinyl"
    assert row["condition"] == "NM/VG+"
    assert row["location"] == "Jazz Shelf"
    assert row["notes"] == "first pressing"
    assert row["_external_ids"] == {"discogs": "12345"}
    assert row["_dedup_external_ids"] == {"discogs": "12345"}
    dg = row["_extra_metadata"]["discogs"]
    assert dg["format"] == "Vinyl, LP, Album, Reissue"
    assert dg["catalog_number"] == "CL 1355"
    assert dg["date_added"] == "2026-01-02 10:00:00"
    assert dg["rating"] == "5"


def test_discogs_strips_artist_disambiguator():
    row, _ = _discogs_to_compendium(_discogs_row(Artist="Nirvana (2)"))
    assert row["_creators"] == [("Nirvana", "artist")]


def test_discogs_2x_vinyl_and_cd():
    assert _discogs_to_compendium(_discogs_row(Format="2xVinyl, LP"))[0]["media_type"] == "vinyl"
    assert _discogs_to_compendium(_discogs_row(Format="CD, Album"))[0]["media_type"] == "cd"


def test_discogs_cassette_row_errors():
    with pytest.raises(ValidationError, match="format"):
        _discogs_to_compendium(_discogs_row(Format="Cassette, Album"))


def test_discogs_empty_title_errors():
    with pytest.raises(ValidationError, match="Title"):
        _discogs_to_compendium(_discogs_row(Title=""))


def test_discogs_empty_released_and_zero():
    assert _discogs_to_compendium(_discogs_row(Released=""))[0]["publication_year"] == ""
    assert _discogs_to_compendium(_discogs_row(Released="0"))[0]["publication_year"] == ""


def test_discogs_empty_conditions():
    row, _ = _discogs_to_compendium(_discogs_row(**{
        "Collection Media Condition": "",
        "Collection Sleeve Condition": "",
    }))
    assert row["condition"] == ""


def test_discogs_media_only_condition():
    row, _ = _discogs_to_compendium(_discogs_row(**{
        "Collection Sleeve Condition": "Not Graded",
    }))
    assert row["condition"] == "NM"


def test_discogs_empty_artist_no_creator():
    row, _ = _discogs_to_compendium(_discogs_row(Artist=""))
    assert row["_creators"] == []


def test_discogs_empty_release_id_no_dedup():
    row, _ = _discogs_to_compendium(_discogs_row(release_id=""))
    assert row["_external_ids"] == {}
    assert row["_dedup_external_ids"] is None
