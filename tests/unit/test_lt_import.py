"""Unit tests for the LibraryThing importer translation layer + encoding helper."""

from __future__ import annotations

import pytest

from compendium.domain.errors import ValidationError
from compendium.services.import_export import (
    _lt_to_compendium,
    decode_text_bytes,
)


def test_decode_clean_utf8_returns_zero_replacements():
    text, replaced = decode_text_bytes("hello é world".encode("utf-8"), strict=False)
    assert text == "hello é world"
    assert replaced == 0


def test_decode_lenient_replaces_stray_byte():
    # 0xe8 is a lone Latin-1 byte (è); invalid as UTF-8 continuation.
    text, replaced = decode_text_bytes(b"hello \xe8 world", strict=False)
    assert replaced == 1
    assert "�" in text


def test_decode_lenient_counts_already_replaced_bytes_too():
    # The U+FFFD char encoded as UTF-8 is \xef\xbf\xbd; if it's already
    # in the file it round-trips through clean UTF-8 and counts.
    data = "Pr�esident".encode("utf-8") + b" \xe8"
    text, replaced = decode_text_bytes(data, strict=False)
    # Two total replacement chars in the result: one was already there,
    # one synthesized. Helper counts U+FFFD in output (best heuristic).
    assert text.count("�") == 2
    assert replaced == 2


def test_decode_strict_raises():
    with pytest.raises(UnicodeDecodeError):
        decode_text_bytes(b"hello \xe8 world", strict=True)


def _lt_row(**overrides):
    base = {
        "Title": "On the Road",
        "Primary Author": "Kerouac, Jack",
        "Secondary Author": "",
        "Publication": "Library of America (2007), Edition: 1st, Hardcover, 864 pages",
        "Date": "2007",
        "Media": "Hardcover",
        "Languages": "English",
        "LC Classification": "PS3521 .E735",
        "Dewey Decimal": "813.54",
        "ISBN": "[1598530127]",
        "Other Call Number": "",
        "Copies": "1",
        "Tags": "",
        "Collections": "",
        "Rating": "",
        "Review": "",
        "Comment": "",
        "Page Count": "",
        "Book Id": "",
        "Work id": "",
        "OCLC": "",
        "LCCN": "",
        "BCID": "",
        "Barcode": "",
    }
    base.update(overrides)
    return base


def test_lt_basic_mapping():
    row, copies = _lt_to_compendium(_lt_row())
    assert copies == 1
    assert row["title"] == "On the Road"
    assert row["authors"] == "Kerouac, Jack"
    assert row["publisher"] == "Library of America"
    assert row["publication_year"] == "2007"
    assert row["media_type"] == "book"
    assert row["language"] == "en"
    assert row["isbn"] == "1598530127"
    assert row["classification_scheme"] == "LCC"
    assert row["classification_code"] == "PS3521 .E735"


def test_lt_authors_join_primary_and_secondary():
    row, _ = _lt_to_compendium(_lt_row(**{"Secondary Author": "Brinkley, Douglas"}))
    assert row["authors"] == "Kerouac, Jack; Brinkley, Douglas"


def test_lt_publication_regex_extracts_publisher_and_year_when_date_blank():
    row, _ = _lt_to_compendium(
        _lt_row(**{"Publication": "Random House (2011), Hardcover, 448 pages", "Date": ""})
    )
    assert row["publisher"] == "Random House"
    assert row["publication_year"] == "2011"


def test_lt_date_takes_precedence_over_publication_year():
    row, _ = _lt_to_compendium(
        _lt_row(**{"Publication": "Bantam (1995), Paperback", "Date": "1996"})
    )
    assert row["publisher"] == "Bantam"
    assert row["publication_year"] == "1996"


def test_lt_publication_without_year_keeps_publisher_blank():
    row, _ = _lt_to_compendium(_lt_row(**{"Publication": "Self-published", "Date": ""}))
    assert row["publisher"] == ""
    assert row["publication_year"] == ""


def test_lt_unknown_date_placeholder_falls_back_to_publication_year():
    # LibraryThing emits "?" or "1850-?" for unknown dates.
    row, _ = _lt_to_compendium(
        _lt_row(**{"Publication": "Penguin (1985), Paperback", "Date": "?"})
    )
    assert row["publication_year"] == "1985"


def test_lt_partial_date_treated_as_unknown():
    row, _ = _lt_to_compendium(_lt_row(**{"Publication": "Penguin (1985)", "Date": "1850-?"}))
    assert row["publication_year"] == "1985"


def test_lt_no_year_anywhere_leaves_publication_year_blank():
    row, _ = _lt_to_compendium(_lt_row(**{"Publication": "Self-published", "Date": "?"}))
    assert row["publication_year"] == ""


def test_lt_isbn_strips_brackets():
    row, _ = _lt_to_compendium(_lt_row(**{"ISBN": "[0940450070]"}))
    assert row["isbn"] == "0940450070"


def test_lt_isbn_passes_through_unbracketed():
    row, _ = _lt_to_compendium(_lt_row(**{"ISBN": "0940450070"}))
    assert row["isbn"] == "0940450070"


def test_lt_isbn_empty_brackets_decode_to_blank():
    # LibraryThing emits "[]" for records without an ISBN.
    row, _ = _lt_to_compendium(_lt_row(**{"ISBN": "[]"}))
    assert row["isbn"] == ""


@pytest.mark.parametrize(
    ("media", "expected"),
    [
        ("Hardcover", "book"),
        ("Paperback", "book"),
        ("Mass Market Paperback", "book"),
        ("Trade Paperback", "book"),
        ("Library Binding", "book"),
        ("Ebook", "book"),
        ("CD", "cd"),
        ("Audiobook (CD)", "cd"),
        ("Vinyl", "vinyl"),
        ("LP", "vinyl"),
        ("DVD", "dvd"),
        ("Blu-ray", "dvd"),
    ],
)
def test_lt_media_mapping(media, expected):
    row, _ = _lt_to_compendium(_lt_row(**{"Media": media}))
    assert row["media_type"] == expected


def test_lt_media_unknown_falls_through_blank():
    row, _ = _lt_to_compendium(_lt_row(**{"Media": "Hologram Cube"}))
    assert row["media_type"] == ""  # caller's default_media_type fills it in


def test_lt_language_english_name_to_iso():
    row, _ = _lt_to_compendium(_lt_row(**{"Languages": "French, German"}))
    assert row["language"] == "fr"


def test_lt_language_unknown_short_passes_through():
    row, _ = _lt_to_compendium(_lt_row(**{"Languages": "xx"}))
    assert row["language"] == "xx"


def test_lt_language_unknown_long_dropped():
    row, _ = _lt_to_compendium(_lt_row(**{"Languages": "Klingonese"}))
    assert row["language"] == ""


def test_lt_classification_lcc_wins_over_ddc():
    row, _ = _lt_to_compendium(_lt_row(**{"LC Classification": "PS123", "Dewey Decimal": "813.54"}))
    assert row["classification_scheme"] == "LCC"
    assert row["classification_code"] == "PS123"


def test_lt_classification_falls_back_to_ddc():
    row, _ = _lt_to_compendium(_lt_row(**{"LC Classification": "", "Dewey Decimal": "813.54"}))
    assert row["classification_scheme"] == "DDC"
    assert row["classification_code"] == "813.54"


def test_lt_classification_neither_leaves_blank():
    row, _ = _lt_to_compendium(_lt_row(**{"LC Classification": "", "Dewey Decimal": ""}))
    assert row["classification_scheme"] == ""
    assert row["classification_code"] == ""


def test_lt_external_ids_collected_when_any_present():
    row, _ = _lt_to_compendium(
        _lt_row(**{"Book Id": "12345", "Work id": "999", "OCLC": "777"})
    )
    assert row["_external_ids"] == {
        "librarything": {"book_id": "12345", "work_id": "999", "oclc": "777"}
    }


def test_lt_external_ids_empty_when_none_present():
    row, _ = _lt_to_compendium(_lt_row())
    assert row["_external_ids"] == {}


def test_lt_extra_metadata_preserves_user_attached_fields():
    row, _ = _lt_to_compendium(
        _lt_row(
            **{
                "Tags": "Fiction, Beat, LOA",
                "Collections": "Your library",
                "Rating": "5",
                "Review": "Loved it",
                "Page Count": "864",
            }
        )
    )
    lt_extra = row["_extra_metadata"]["librarything"]
    assert lt_extra["tags"] == ["Fiction", "Beat", "LOA"]
    assert lt_extra["collections"] == ["Your library"]
    assert lt_extra["rating"] == "5"
    assert lt_extra["review"] == "Loved it"
    assert lt_extra["page_count"] == "864"


def test_lt_extra_metadata_empty_when_no_user_fields():
    row, _ = _lt_to_compendium(_lt_row())
    assert row["_extra_metadata"] == {}


def test_lt_copies_parsed_to_int_with_floor_of_1():
    row, copies = _lt_to_compendium(_lt_row(**{"Copies": "3"}))
    assert copies == 3
    row, copies = _lt_to_compendium(_lt_row(**{"Copies": "0"}))
    assert copies == 1
    row, copies = _lt_to_compendium(_lt_row(**{"Copies": ""}))
    assert copies == 1
    row, copies = _lt_to_compendium(_lt_row(**{"Copies": "garbage"}))
    assert copies == 1


def test_lt_missing_title_raises():
    with pytest.raises(ValidationError):
        _lt_to_compendium(_lt_row(**{"Title": ""}))
