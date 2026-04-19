import re
from typing import Protocol, runtime_checkable

import httpx

from compendium.domain.errors import ExternalLookupError, ValidationError

_OPENLIBRARY_URL = "https://openlibrary.org/api/books"
_MB_BASE = "https://musicbrainz.org/ws/2"
_MB_UA = "Compendium/0.1.0 (open-source library catalog)"


# ---------------------------------------------------------------------------
# Identifier normalisation
# ---------------------------------------------------------------------------

def normalize_isbn(raw: str) -> str:
    isbn = re.sub(r"[\s\-]", "", raw)
    if len(isbn) == 10:
        isbn = _isbn10_to_13(isbn)
    if len(isbn) != 13 or not isbn.isdigit():
        raise ValidationError(f"'{raw}' is not a valid ISBN-10 or ISBN-13")
    return isbn


def normalize_upc(raw: str) -> str:
    upc = re.sub(r"[\s\-]", "", raw)
    if not upc.isdigit() or len(upc) not in (8, 12, 13):
        raise ValidationError(f"'{raw}' is not a valid UPC/EAN barcode")
    return upc


def _isbn10_to_13(isbn10: str) -> str:
    digits = "978" + isbn10[:9]
    check = (10 - sum((i % 2 * 2 + 1) * int(d) for i, d in enumerate(digits)) % 10) % 10
    return digits + str(check)


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class MetadataAdapter(Protocol):
    def lookup(self, kind: str, value: str) -> dict | None: ...


# ---------------------------------------------------------------------------
# Open Library adapter (books)
# ---------------------------------------------------------------------------

def lookup_isbn(isbn: str) -> dict:
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
        "creator_role": "author",
        "publisher": publishers[0] if publishers else None,
        "publication_year": year,
        "description": (
            (data.get("notes") or {}).get("value")
            if isinstance(data.get("notes"), dict)
            else data.get("notes")
        ),
        "cover_image_url": cover_url,
        "isbn": isbn,
        "upc": None,
        "external_ids": {"openlibrary": ol_id} if ol_id else {},
        "extra_metadata": {},
    }


class OpenLibraryAdapter:
    def lookup(self, kind: str, value: str) -> dict | None:
        if kind != "isbn":
            raise ExternalLookupError(f"Open Library does not support identifier kind '{kind}'")
        data = lookup_isbn(value)
        if not data:
            return None
        return parse_open_library(data, value)


# ---------------------------------------------------------------------------
# MusicBrainz adapter (vinyl, CD)
# ---------------------------------------------------------------------------

_MBID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def _mb_get(path: str, params: dict | None = None) -> dict:
    headers = {"User-Agent": _MB_UA, "Accept": "application/json"}
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{_MB_BASE}/{path}", params=params, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExternalLookupError(f"MusicBrainz request failed: {exc}") from exc
    return resp.json()


def _mb_lookup_by_upc(upc: str) -> dict | None:
    data = _mb_get("release", {"query": f"barcode:{upc}", "fmt": "json", "limit": "1"})
    releases = data.get("releases", [])
    if not releases:
        return None
    mbid = releases[0]["id"]
    return _mb_fetch_release(mbid, upc)


def _mb_lookup_by_mbid(mbid: str) -> dict | None:
    return _mb_fetch_release(mbid, upc=None)


def _mb_fetch_release(mbid: str, upc: str | None) -> dict | None:
    data = _mb_get(
        f"release/{mbid}",
        {"inc": "recordings artist-credits labels", "fmt": "json"},
    )
    return _parse_mb_release(data, upc or data.get("barcode") or "")


def _parse_mb_release(data: dict, upc: str) -> dict:
    artists = [
        ac["artist"]["name"]
        for ac in data.get("artist-credit", [])
        if isinstance(ac, dict) and "artist" in ac
    ]

    label_info = data.get("label-info", [])
    publisher = (
        label_info[0]["label"]["name"]
        if label_info and isinstance(label_info[0].get("label"), dict)
        else None
    )

    date_str = data.get("date", "")
    year: int | None = None
    m = re.search(r"\d{4}", date_str)
    if m:
        year = int(m.group())

    media_list = data.get("media", [])
    fmt = media_list[0].get("format", "") if media_list else ""

    tracks = []
    for medium in media_list:
        for track in medium.get("tracks", []):
            recording = track.get("recording") or {}
            tracks.append({
                "position": track.get("position"),
                "title": track.get("title") or recording.get("title", ""),
                "length_ms": track.get("length"),
            })

    return {
        "title": data.get("title", "Unknown Title"),
        "subtitle": None,
        "authors": artists,
        "creator_role": "artist",
        "publisher": publisher,
        "publication_year": year,
        "description": None,
        "cover_image_url": None,
        "isbn": None,
        "upc": upc or None,
        "external_ids": {"musicbrainz": data.get("id", "")},
        "extra_metadata": {
            "format": fmt,
            "tracks": tracks,
            "track_count": len(tracks),
        },
    }


class MusicBrainzAdapter:
    def lookup(self, kind: str, value: str) -> dict | None:
        if kind == "upc":
            return _mb_lookup_by_upc(value)
        if kind == "mbid":
            return _mb_lookup_by_mbid(value)
        raise ExternalLookupError(f"MusicBrainz does not support identifier kind '{kind}'")


# ---------------------------------------------------------------------------
# Registry and dispatcher
# ---------------------------------------------------------------------------

_ADAPTERS: dict[str, MetadataAdapter] = {
    "book": OpenLibraryAdapter(),
    "vinyl": MusicBrainzAdapter(),
    "cd": MusicBrainzAdapter(),
}


def lookup_metadata(media_type_code: str, kind: str, value: str) -> dict | None:
    adapter = _ADAPTERS.get(media_type_code)
    if adapter is None:
        raise ExternalLookupError(
            f"No metadata adapter for media type '{media_type_code}'. "
            "Use manual entry for this type."
        )
    return adapter.lookup(kind, value)
