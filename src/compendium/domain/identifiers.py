"""Barcode / identifier minting and validation for Compendium.

Format
------
10-digit:  [type][slug x8][check]
14-digit:  [type][loc x4][slug x8][check]

Type prefix:  2 = patron card,  3 = item barcode
Slug:         8 decimal digits, globally unique within type
Check digit:  mod-10 Luhn, encoding-neutral (works with Code 128, Code 39, Codabar)

Both lengths coexist permanently in any deployment; the `barcode_format`
setting controls whether newly minted codes are 10-digit or 14-digit;
existing barcodes are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass

ITEM_TYPE = 3
PATRON_TYPE = 2


@dataclass(frozen=True)
class ParsedBarcode:
    type: int          # ITEM_TYPE or PATRON_TYPE
    location_code: str | None  # 4-digit string if 14-digit format, else None
    slug: str          # 8-digit unique identifier
    check: int         # Luhn check digit


def luhn_check_digit(digits: str) -> int:
    """Return the Luhn check digit for *digits* (payload without the check).

    When the returned digit is appended to *digits*, the full string satisfies
    the Luhn algorithm (sum of all processed digits ≡ 0 mod 10).
    """
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        # Rightmost payload digit lands at position 2 from right after appending
        # the check → gets doubled.  Alternate from there leftward.
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return (10 - (total % 10)) % 10


def validate_barcode(s: str, *, expected_type: int | None = None) -> ParsedBarcode | None:
    """Parse and validate *s* as a Compendium barcode.

    Returns a ``ParsedBarcode`` on success, ``None`` if the string does not
    conform to the format (wrong length, non-digits, unknown type prefix, or
    bad Luhn check).

    Pass ``expected_type=ITEM_TYPE`` or ``expected_type=PATRON_TYPE`` to also
    reject strings with a mismatched type prefix.
    """
    if not s.isdigit():
        return None

    n = len(s)
    if n == 10:
        type_digit = int(s[0])
        location_code: str | None = None
        slug = s[1:9]
        check = int(s[9])
    elif n == 14:
        type_digit = int(s[0])
        location_code = s[1:5]
        slug = s[5:13]
        check = int(s[13])
    else:
        return None

    if type_digit not in (ITEM_TYPE, PATRON_TYPE):
        return None

    if expected_type is not None and type_digit != expected_type:
        return None

    if luhn_check_digit(s[:-1]) != check:
        return None

    return ParsedBarcode(type=type_digit, location_code=location_code, slug=slug, check=check)


def format_item_barcode(slug: str, *, location_code: str | None) -> str:
    """Format an 8-digit *slug* as a 10- or 14-digit item barcode.

    Pass ``location_code=None`` for 10-digit format; pass a 4-digit string for
    14-digit format.
    """
    if location_code is not None:
        payload = f"{ITEM_TYPE}{location_code}{slug}"
    else:
        payload = f"{ITEM_TYPE}{slug}"
    return f"{payload}{luhn_check_digit(payload)}"


def format_patron_card(slug: str, *, location_code: str | None) -> str:
    """Format an 8-digit *slug* as a 10- or 14-digit patron card number.

    Pass ``location_code=None`` for 10-digit format; pass a 4-digit string for
    14-digit format.
    """
    if location_code is not None:
        payload = f"{PATRON_TYPE}{location_code}{slug}"
    else:
        payload = f"{PATRON_TYPE}{slug}"
    return f"{payload}{luhn_check_digit(payload)}"
