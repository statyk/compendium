import re

import httpx

from compendium.domain.errors import ExternalLookupError, ValidationError

_OPENLIBRARY_URL = "https://openlibrary.org/api/books"


def normalize_isbn(raw: str) -> str:
    """Strip hyphens/spaces and validate; return the normalized ISBN-13 string."""
    isbn = re.sub(r"[\s\-]", "", raw)
    if len(isbn) == 10:
        isbn = _isbn10_to_13(isbn)
    if len(isbn) != 13 or not isbn.isdigit():
        raise ValidationError(f"'{raw}' is not a valid ISBN-10 or ISBN-13")
    return isbn


def _isbn10_to_13(isbn10: str) -> str:
    digits = "978" + isbn10[:9]
    check = (10 - sum((i % 2 * 2 + 1) * int(d) for i, d in enumerate(digits)) % 10) % 10
    return digits + str(check)


def lookup_isbn(isbn: str) -> dict:
    """Fetch metadata for a single ISBN from Open Library.

    Returns the parsed data dict (may be empty if the ISBN is unknown).
    Raises ExternalLookupError on network or HTTP failure.
    """
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                _OPENLIBRARY_URL,
                params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExternalLookupError(f"Open Library request failed: {exc}") from exc

    return resp.json().get(f"ISBN:{isbn}", {})


def parse_open_library(data: dict, isbn: str) -> dict:
    """Convert a raw Open Library response into a normalised metadata dict."""
    authors = [a.get("name", "") for a in data.get("authors", [])]
    publishers = [p.get("name", "") for p in data.get("publishers", [])]
    cover_url = data.get("cover", {}).get("large") or data.get("cover", {}).get("medium")

    identifiers = data.get("identifiers", {})
    ol_id = identifiers.get("openlibrary", [None])[0]

    pub_date: str = data.get("publish_date", "")
    year: int | None = None
    m = re.search(r"\d{4}", pub_date)
    if m:
        year = int(m.group())

    return {
        "title": data.get("title", "Unknown Title"),
        "subtitle": data.get("subtitle"),
        "authors": authors,
        "publisher": publishers[0] if publishers else None,
        "publication_year": year,
        "description": (data.get("notes") or {}).get("value") if isinstance(data.get("notes"), dict) else data.get("notes"),
        "cover_image_url": cover_url,
        "external_ids": {"openlibrary": ol_id} if ol_id else {},
        "isbn": isbn,
    }
