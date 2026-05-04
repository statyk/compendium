"""Tests for compendium.domain.identifiers — pure Luhn + format/parse logic."""

import pytest

from compendium.domain.identifiers import (
    ITEM_TYPE,
    PATRON_TYPE,
    ParsedBarcode,
    format_item_barcode,
    format_patron_card,
    luhn_check_digit,
    validate_barcode,
)


# ---------------------------------------------------------------------------
# luhn_check_digit
# ---------------------------------------------------------------------------


def test_luhn_known_credit_card():
    # 4532015112830366 is a well-known Luhn-valid test card; payload = first 15 digits
    assert luhn_check_digit("453201511283036") == 6


def test_luhn_all_zeros_payload():
    assert luhn_check_digit("0" * 9) == 0


def test_luhn_single_digit_zero():
    assert luhn_check_digit("0") == 0


def test_luhn_single_digit_one():
    # payload "1": doubled → 2; total=2; check=(10-2)%10=8
    assert luhn_check_digit("1") == 8


def test_luhn_check_appended_validates():
    payload = "300000012"
    check = luhn_check_digit(payload)
    full = payload + str(check)
    # Full string must sum to 0 mod 10 under Luhn
    total = 0
    for i, ch in enumerate(reversed(full)):
        n = int(ch)
        if i % 2 != 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    assert total % 10 == 0


# ---------------------------------------------------------------------------
# format_item_barcode / format_patron_card
# ---------------------------------------------------------------------------


def test_format_item_barcode_10_digit():
    code = format_item_barcode("00000001", location_code=None)
    assert len(code) == 10
    assert code[0] == str(ITEM_TYPE)
    assert code[1:9] == "00000001"


def test_format_patron_card_10_digit():
    code = format_patron_card("00000001", location_code=None)
    assert len(code) == 10
    assert code[0] == str(PATRON_TYPE)
    assert code[1:9] == "00000001"


def test_format_item_barcode_14_digit():
    code = format_item_barcode("00000001", location_code="0001")
    assert len(code) == 14
    assert code[0] == str(ITEM_TYPE)
    assert code[1:5] == "0001"
    assert code[5:13] == "00000001"


def test_format_patron_card_14_digit():
    code = format_patron_card("00000001", location_code="0001")
    assert len(code) == 14
    assert code[0] == str(PATRON_TYPE)
    assert code[1:5] == "0001"
    assert code[5:13] == "00000001"


def test_format_item_different_from_patron_same_slug():
    item = format_item_barcode("12345678", location_code=None)
    patron = format_patron_card("12345678", location_code=None)
    assert item != patron
    assert item[0] == str(ITEM_TYPE)
    assert patron[0] == str(PATRON_TYPE)


# ---------------------------------------------------------------------------
# validate_barcode — round-trip
# ---------------------------------------------------------------------------


def test_validate_roundtrip_item_10():
    code = format_item_barcode("12345678", location_code=None)
    result = validate_barcode(code)
    assert result == ParsedBarcode(
        type=ITEM_TYPE, location_code=None, slug="12345678", check=int(code[-1])
    )


def test_validate_roundtrip_patron_10():
    code = format_patron_card("99999999", location_code=None)
    result = validate_barcode(code)
    assert result is not None
    assert result.type == PATRON_TYPE
    assert result.slug == "99999999"
    assert result.location_code is None


def test_validate_roundtrip_item_14():
    code = format_item_barcode("00000001", location_code="0001")
    result = validate_barcode(code)
    assert result is not None
    assert result.type == ITEM_TYPE
    assert result.location_code == "0001"
    assert result.slug == "00000001"


def test_validate_roundtrip_patron_14():
    code = format_patron_card("00000001", location_code="9999")
    result = validate_barcode(code)
    assert result is not None
    assert result.type == PATRON_TYPE
    assert result.location_code == "9999"
    assert result.slug == "00000001"


# ---------------------------------------------------------------------------
# validate_barcode — expected_type filter
# ---------------------------------------------------------------------------


def test_validate_expected_type_match():
    code = format_item_barcode("12345678", location_code=None)
    assert validate_barcode(code, expected_type=ITEM_TYPE) is not None


def test_validate_expected_type_mismatch():
    code = format_item_barcode("12345678", location_code=None)
    assert validate_barcode(code, expected_type=PATRON_TYPE) is None


def test_validate_patron_expected_type_match():
    code = format_patron_card("12345678", location_code=None)
    assert validate_barcode(code, expected_type=PATRON_TYPE) is not None


# ---------------------------------------------------------------------------
# validate_barcode — rejection cases
# ---------------------------------------------------------------------------


def test_validate_rejects_non_digits():
    assert validate_barcode("30000001X3") is None


def test_validate_rejects_wrong_length_short():
    assert validate_barcode("300000013") is None  # 9 digits


def test_validate_rejects_wrong_length_11():
    assert validate_barcode("30000000013") is None  # 11 digits


def test_validate_rejects_wrong_length_13():
    assert validate_barcode("3000000001234") is None  # 13 digits


def test_validate_rejects_bad_check_digit():
    code = format_item_barcode("12345678", location_code=None)
    # Flip the last digit
    bad = code[:-1] + str((int(code[-1]) + 1) % 10)
    assert validate_barcode(bad) is None


def test_validate_rejects_unknown_type_prefix():
    # prefix 1 is neither ITEM_TYPE nor PATRON_TYPE
    code = "1" + "0" * 8
    check = luhn_check_digit(code)
    assert validate_barcode(code + str(check)) is None


def test_validate_rejects_type_4_prefix_14_digit():
    payload = "4" + "0001" + "12345678"
    code = payload + str(luhn_check_digit(payload))
    assert validate_barcode(code) is None


def test_validate_empty_string():
    assert validate_barcode("") is None


# ---------------------------------------------------------------------------
# Boundary values — min and max slugs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", ["00000000", "99999999"])
def test_boundary_slugs_10_digit(slug):
    code = format_item_barcode(slug, location_code=None)
    result = validate_barcode(code, expected_type=ITEM_TYPE)
    assert result is not None
    assert result.slug == slug


@pytest.mark.parametrize("slug", ["00000000", "99999999"])
def test_boundary_slugs_14_digit(slug):
    code = format_item_barcode(slug, location_code="0000")
    result = validate_barcode(code, expected_type=ITEM_TYPE)
    assert result is not None
    assert result.slug == slug
    assert result.location_code == "0000"


@pytest.mark.parametrize("loc", ["0000", "9999"])
def test_boundary_location_codes(loc):
    code = format_patron_card("12345678", location_code=loc)
    result = validate_barcode(code, expected_type=PATRON_TYPE)
    assert result is not None
    assert result.location_code == loc
