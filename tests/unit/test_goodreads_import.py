"""Unit tests for the GoodReads CSV importer translation layer."""

from __future__ import annotations

import pytest

from compendium.domain.errors import ValidationError
from compendium.services.import_export import _gr_to_compendium, _gr_unwrap_isbn


# ── ISBN unwrapper ────────────────────────────────────────────────────────────


def test_unwrap_isbn_strips_excel_notation():
    assert _gr_unwrap_isbn('="0140067477"') == "0140067477"


def test_unwrap_isbn_strips_isbn13():
    assert _gr_unwrap_isbn('="9780140067477"') == "9780140067477"


def test_unwrap_isbn_empty_excel_notation_returns_none():
    assert _gr_unwrap_isbn('=""') is None


def test_unwrap_isbn_bare_value_passes_through():
    assert _gr_unwrap_isbn("0140067477") == "0140067477"


def test_unwrap_isbn_none_returns_none():
    assert _gr_unwrap_isbn(None) is None


def test_unwrap_isbn_empty_string_returns_none():
    assert _gr_unwrap_isbn("") is None


# ── Row fixture ───────────────────────────────────────────────────────────────


def _gr_row(**overrides):
    base = {
        "Book Id": "242337159",
        "Title": "The Tao of Pooh",
        "Author": "Benjamin Hoff",
        "Author l-f": "Hoff, Benjamin",
        "Additional Authors": "",
        "ISBN": '="0140067477"',
        "ISBN13": '="9780140067477"',
        "My Rating": "4",
        "Publisher": "Penguin Books",
        "Binding": "Paperback",
        "Number of Pages": "158",
        "Year Published": "1983",
        "Original Publication Year": "1982",
        "Date Read": "2026/01/15",
        "Date Added": "2026/05/08",
        "Bookshelves": "philosophy,favorites",
        "Bookshelves with positions": "philosophy (#3), favorites (#1)",
        "Exclusive Shelf": "read",
        "My Review": "Delightful little book.",
        "Spoiler": "",
        "Private Notes": "",
        "Read Count": "1",
        "Owned Copies": "1",
    }
    base.update(overrides)
    return base


# ── Basic mapping ─────────────────────────────────────────────────────────────


def test_gr_basic_mapping():
    row, copies = _gr_to_compendium(_gr_row())
    assert copies == 1
    assert row["title"] == "The Tao of Pooh"
    assert row["_creators"] == [("Benjamin Hoff", "author")]
    assert row["publisher"] == "Penguin Books"
    assert row["publication_year"] == "1983"
    assert row["media_type"] == "book"
    assert row["isbn"] == "9780140067477"  # ISBN13 preferred


def test_gr_always_book_media_type():
    row, _ = _gr_to_compendium(_gr_row(**{"Binding": "Kindle Edition"}))
    assert row["media_type"] == "book"


def test_gr_language_is_empty():
    row, _ = _gr_to_compendium(_gr_row())
    assert row["language"] == ""


def test_gr_classification_empty():
    row, _ = _gr_to_compendium(_gr_row())
    assert row["classification_scheme"] == ""
    assert row["classification_code"] == ""


def test_gr_barcode_empty():
    row, _ = _gr_to_compendium(_gr_row())
    assert row["barcode"] == ""


# ── Author / creator handling ─────────────────────────────────────────────────


def test_gr_additional_authors_mapped_to_contributor():
    row, _ = _gr_to_compendium(
        _gr_row(**{"Additional Authors": "Brock Book Design, Jane Smith"})
    )
    assert ("Benjamin Hoff", "author") in row["_creators"]
    assert ("Brock Book Design", "contributor") in row["_creators"]
    assert ("Jane Smith", "contributor") in row["_creators"]
    assert len(row["_creators"]) == 3


def test_gr_empty_additional_authors_omitted():
    row, _ = _gr_to_compendium(_gr_row(**{"Additional Authors": ""}))
    assert row["_creators"] == [("Benjamin Hoff", "author")]


def test_gr_missing_primary_author_produces_no_creator():
    row, _ = _gr_to_compendium(_gr_row(**{"Author": ""}))
    assert row["_creators"] == []


# ── ISBN handling ─────────────────────────────────────────────────────────────


def test_gr_prefers_isbn13_over_isbn10():
    row, _ = _gr_to_compendium(
        _gr_row(**{"ISBN": '="0140067477"', "ISBN13": '="9780140067477"'})
    )
    assert row["isbn"] == "9780140067477"


def test_gr_falls_back_to_isbn10_when_isbn13_empty():
    row, _ = _gr_to_compendium(
        _gr_row(**{"ISBN": '="0140067477"', "ISBN13": '=""'})
    )
    assert row["isbn"] == "0140067477"


def test_gr_isbn_empty_when_both_absent():
    row, _ = _gr_to_compendium(_gr_row(**{"ISBN": '=""', "ISBN13": '=""'}))
    assert row["isbn"] == ""


def test_gr_isbn_bare_value_passes_through():
    row, _ = _gr_to_compendium(
        _gr_row(**{"ISBN": "0140067477", "ISBN13": ""})
    )
    assert row["isbn"] == "0140067477"


# ── Owned Copies → copies count ───────────────────────────────────────────────


def test_gr_owned_copies_zero_gives_one():
    _, copies = _gr_to_compendium(_gr_row(**{"Owned Copies": "0"}))
    assert copies == 1


def test_gr_owned_copies_empty_gives_one():
    _, copies = _gr_to_compendium(_gr_row(**{"Owned Copies": ""}))
    assert copies == 1


def test_gr_owned_copies_one_gives_one():
    _, copies = _gr_to_compendium(_gr_row(**{"Owned Copies": "1"}))
    assert copies == 1


def test_gr_owned_copies_three_gives_three():
    _, copies = _gr_to_compendium(_gr_row(**{"Owned Copies": "3"}))
    assert copies == 3


def test_gr_owned_copies_non_numeric_gives_one():
    _, copies = _gr_to_compendium(_gr_row(**{"Owned Copies": "many"}))
    assert copies == 1


# ── extra_metadata.goodreads ──────────────────────────────────────────────────


def test_gr_bookshelves_split_into_list():
    row, _ = _gr_to_compendium(_gr_row(**{"Bookshelves": "philosophy,favorites"}))
    assert row["_extra_metadata"]["goodreads"]["bookshelves"] == [
        "philosophy",
        "favorites",
    ]


def test_gr_empty_bookshelves_omitted():
    row, _ = _gr_to_compendium(_gr_row(**{"Bookshelves": ""}))
    gr = row["_extra_metadata"].get("goodreads", {})
    assert "bookshelves" not in gr


def test_gr_exclusive_shelf_preserved():
    row, _ = _gr_to_compendium(_gr_row(**{"Exclusive Shelf": "to-read"}))
    assert row["_extra_metadata"]["goodreads"]["exclusive_shelf"] == "to-read"


def test_gr_rating_preserved():
    row, _ = _gr_to_compendium(_gr_row(**{"My Rating": "5"}))
    assert row["_extra_metadata"]["goodreads"]["rating"] == "5"


def test_gr_review_preserved():
    row, _ = _gr_to_compendium(_gr_row(**{"My Review": "A great read."}))
    assert row["_extra_metadata"]["goodreads"]["review"] == "A great read."


def test_gr_binding_preserved():
    row, _ = _gr_to_compendium(_gr_row(**{"Binding": "Mass Market Paperback"}))
    assert row["_extra_metadata"]["goodreads"]["binding"] == "Mass Market Paperback"


def test_gr_original_publication_year_preserved():
    row, _ = _gr_to_compendium(_gr_row(**{"Original Publication Year": "1937"}))
    assert (
        row["_extra_metadata"]["goodreads"]["original_publication_year"] == "1937"
    )


def test_gr_page_count_preserved():
    row, _ = _gr_to_compendium(_gr_row(**{"Number of Pages": "158"}))
    assert row["_extra_metadata"]["goodreads"]["page_count"] == "158"


def test_gr_date_read_and_added_preserved():
    row, _ = _gr_to_compendium(
        _gr_row(**{"Date Read": "2025/06/01", "Date Added": "2025/05/01"})
    )
    gr = row["_extra_metadata"]["goodreads"]
    assert gr["date_read"] == "2025/06/01"
    assert gr["date_added"] == "2025/05/01"


def test_gr_read_count_preserved():
    row, _ = _gr_to_compendium(_gr_row(**{"Read Count": "2"}))
    assert row["_extra_metadata"]["goodreads"]["read_count"] == "2"


def test_gr_no_extra_metadata_when_all_empty():
    row, _ = _gr_to_compendium(
        _gr_row(
            **{
                "My Rating": "",
                "My Review": "",
                "Binding": "",
                "Number of Pages": "",
                "Year Published": "",
                "Original Publication Year": "",
                "Date Read": "",
                "Date Added": "",
                "Bookshelves": "",
                "Exclusive Shelf": "",
                "Private Notes": "",
                "Read Count": "",
            }
        )
    )
    assert "goodreads" not in row.get("_extra_metadata", {})


# ── external_ids ──────────────────────────────────────────────────────────────


def test_gr_book_id_in_external_ids():
    row, _ = _gr_to_compendium(_gr_row(**{"Book Id": "242337159"}))
    assert row["_external_ids"]["goodreads"]["book_id"] == "242337159"


def test_gr_no_external_ids_when_book_id_empty():
    row, _ = _gr_to_compendium(_gr_row(**{"Book Id": ""}))
    assert "goodreads" not in row.get("_external_ids", {})


# ── Year handling ─────────────────────────────────────────────────────────────


def test_gr_year_published_four_digit_accepted():
    row, _ = _gr_to_compendium(_gr_row(**{"Year Published": "2023"}))
    assert row["publication_year"] == "2023"


def test_gr_year_published_empty_gives_empty_string():
    row, _ = _gr_to_compendium(_gr_row(**{"Year Published": ""}))
    assert row["publication_year"] == ""


def test_gr_year_published_non_numeric_gives_empty_string():
    row, _ = _gr_to_compendium(_gr_row(**{"Year Published": "unknown"}))
    assert row["publication_year"] == ""


# ── Validation ────────────────────────────────────────────────────────────────


def test_gr_missing_title_raises():
    with pytest.raises(ValidationError, match="Title"):
        _gr_to_compendium(_gr_row(**{"Title": ""}))


def test_gr_whitespace_only_title_raises():
    with pytest.raises(ValidationError, match="Title"):
        _gr_to_compendium(_gr_row(**{"Title": "   "}))
