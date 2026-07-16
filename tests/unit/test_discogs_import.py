"""Unit tests for the Discogs CSV importer translation layer."""
from __future__ import annotations

import pytest

from compendium.domain.errors import ValidationError
from compendium.services.import_export import (
    _discogs_grade,
    _discogs_media_type,
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
