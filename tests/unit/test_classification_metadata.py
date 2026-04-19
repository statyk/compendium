"""Unit tests for classification extraction from Open Library metadata."""

from compendium.services.metadata import parse_open_library

_BASE_OL = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}


def test_parse_open_library_extracts_lc_classification():
    data = {**_BASE_OL, "classifications": {"lc_classifications": ["PS3558.E63 D8"]}}
    meta = parse_open_library(data, "9780441013593")
    assert meta["lc_classification"] == "PS3558.E63 D8"


def test_parse_open_library_extracts_ddc_classification():
    data = {**_BASE_OL, "classifications": {"dewey_decimal_class": ["813.54"]}}
    meta = parse_open_library(data, "9780441013593")
    assert meta["ddc_classification"] == "813.54"


def test_parse_open_library_extracts_both_classifications():
    data = {
        **_BASE_OL,
        "classifications": {
            "lc_classifications": ["PS3558.E63 D8"],
            "dewey_decimal_class": ["813.54"],
        },
    }
    meta = parse_open_library(data, "9780441013593")
    assert meta["lc_classification"] == "PS3558.E63 D8"
    assert meta["ddc_classification"] == "813.54"


def test_parse_open_library_no_classifications_returns_none():
    meta = parse_open_library(_BASE_OL, "9780441013593")
    assert meta["lc_classification"] is None
    assert meta["ddc_classification"] is None


def test_parse_open_library_empty_classifications_returns_none():
    data = {**_BASE_OL, "classifications": {}}
    meta = parse_open_library(data, "9780441013593")
    assert meta["lc_classification"] is None
    assert meta["ddc_classification"] is None


def test_parse_open_library_uses_first_lc_classification():
    data = {
        **_BASE_OL,
        "classifications": {"lc_classifications": ["PS3558.E63 D8", "PS3558.E63 D8 1965"]},
    }
    meta = parse_open_library(data, "9780441013593")
    assert meta["lc_classification"] == "PS3558.E63 D8"
