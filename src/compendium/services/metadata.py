import os
import re
import xml.etree.ElementTree as ET
from typing import Protocol, runtime_checkable

import httpx

from compendium.domain.errors import ExternalLookupError, ValidationError

_OPENLIBRARY_URL = "https://openlibrary.org/api/books"
_LOC_LCCN_URL = "https://lccn.loc.gov/{lccn}/marcxml"
_LOC_SRU_URL = "https://lx2.loc.gov/sru/catalog"
_MARC_NS = "http://www.loc.gov/MARC21/slim"
_MB_BASE = "https://musicbrainz.org/ws/2"
_MB_UA = "Compendium/0.1.0 (open-source library catalog)"
_TMDB_BASE = "https://api.themoviedb.org/3"
_TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


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
    lccn_list = identifiers.get("lccn", [])
    lccn: str | None = lccn_list[0] if lccn_list else None

    pub_date: str = data.get("publish_date", "")
    year: int | None = None
    m = re.search(r"\d{4}", pub_date)
    if m:
        year = int(m.group())

    classifications = data.get("classifications", {}) or {}
    lc_list = classifications.get("lc_classifications", [])
    ddc_list = classifications.get("dewey_decimal_class", [])
    lc_classification: str | None = lc_list[0] if lc_list else None
    ddc_classification: str | None = ddc_list[0] if ddc_list else None

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
        "lc_classification": lc_classification,
        "ddc_classification": ddc_classification,
        "lccn": lccn,
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
# Library of Congress — LCC fallback lookup
# ---------------------------------------------------------------------------

def _parse_lcc_from_marcxml(xml_text: str) -> str | None:
    """Extract LCC call number from MARC XML (field 050, subfields a+b).

    Works for both the bare LCCN permalink response and the SRU wrapper.
    """
    try:
        root = ET.fromstring(xml_text)
        for df in root.iter(f"{{{_MARC_NS}}}datafield"):
            if df.get("tag") == "050":
                a = df.find(f"{{{_MARC_NS}}}subfield[@code='a']")
                b = df.find(f"{{{_MARC_NS}}}subfield[@code='b']")
                if a is not None and a.text:
                    parts = [a.text.strip()]
                    if b is not None and b.text:
                        parts.append(b.text.strip())
                    return " ".join(parts)
    except ET.ParseError:
        return None
    return None


def _try_lcc_by_lccn(lccn: str) -> str | None:
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.get(_LOC_LCCN_URL.format(lccn=lccn))
            if resp.status_code != 200:
                return None
            return _parse_lcc_from_marcxml(resp.text)
    except Exception:
        return None


def _try_lcc_by_isbn(isbn: str) -> str | None:
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.get(
                _LOC_SRU_URL,
                params={
                    "version": "1.1",
                    "operation": "searchRetrieve",
                    "recordSchema": "marcxml",
                    "maximumRecords": "1",
                    "query": f"bath.isbn={isbn}",
                },
            )
            if resp.status_code != 200:
                return None
            return _parse_lcc_from_marcxml(resp.text)
    except Exception:
        return None


def lookup_lcc_from_loc(isbn: str, lccn: str | None = None) -> str | None:
    """Fetch LCC call number from Library of Congress as a fallback.

    Tries the LCCN permalink MARCXML first (faster, more reliable when LCCN is
    available), then falls back to SRU ISBN query.  Returns None on any failure.
    """
    if lccn:
        result = _try_lcc_by_lccn(lccn)
        if result:
            return result
    if isbn:
        return _try_lcc_by_isbn(isbn)
    return None


def _parse_ddc_from_marcxml(xml_text: str) -> str | None:
    """Extract DDC number from MARC XML (field 082, subfield a)."""
    try:
        root = ET.fromstring(xml_text)
        for df in root.iter(f"{{{_MARC_NS}}}datafield"):
            if df.get("tag") == "082":
                a = df.find(f"{{{_MARC_NS}}}subfield[@code='a']")
                if a is not None and a.text:
                    return a.text.strip()
    except ET.ParseError:
        return None
    return None


def _try_ddc_by_lccn(lccn: str) -> str | None:
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.get(_LOC_LCCN_URL.format(lccn=lccn))
            if resp.status_code != 200:
                return None
            return _parse_ddc_from_marcxml(resp.text)
    except Exception:
        return None


def _try_ddc_by_isbn(isbn: str) -> str | None:
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.get(
                _LOC_SRU_URL,
                params={
                    "version": "1.1",
                    "operation": "searchRetrieve",
                    "recordSchema": "marcxml",
                    "maximumRecords": "1",
                    "query": f"bath.isbn={isbn}",
                },
            )
            if resp.status_code != 200:
                return None
            return _parse_ddc_from_marcxml(resp.text)
    except Exception:
        return None


def lookup_ddc_from_loc(isbn: str, lccn: str | None = None) -> str | None:
    """Fetch DDC number from Library of Congress as a fallback.

    Tries the LCCN permalink MARCXML first, then falls back to SRU ISBN query.
    Returns None on any failure.
    """
    if lccn:
        result = _try_ddc_by_lccn(lccn)
        if result:
            return result
    if isbn:
        return _try_ddc_by_isbn(isbn)
    return None


def pick_classification_code(scheme: str, meta: dict) -> str | None:
    """Resolve a classification code for the given scheme from metadata.

    Prefers the number supplied by the metadata source (e.g. Open Library);
    falls back to a Library of Congress lookup when the source lacks it.
    Returns None for ``scheme == "none"`` or when no number is available.
    """
    if scheme == "lcc":
        return meta.get("lc_classification") or lookup_lcc_from_loc(
            isbn=meta.get("isbn") or "", lccn=meta.get("lccn")
        )
    if scheme == "ddc":
        return meta.get("ddc_classification") or lookup_ddc_from_loc(
            isbn=meta.get("isbn") or "", lccn=meta.get("lccn")
        )
    return None


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
# TMDb adapter (dvd, bluray, vhs)
# ---------------------------------------------------------------------------

def _tmdb_get(path: str, params: dict, api_key: str) -> dict:
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{_TMDB_BASE}/{path}",
                params={"api_key": api_key, **params},
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExternalLookupError(f"TMDb request failed: {exc}") from exc
    return resp.json()


def _tmdb_search_candidates(query: str, api_key: str) -> list[dict]:
    data = _tmdb_get("search/movie", {"query": query, "language": "en-US"}, api_key)
    candidates = []
    for r in data.get("results", [])[:10]:
        poster = f"{_TMDB_IMAGE_BASE}{r['poster_path']}" if r.get("poster_path") else None
        release = r.get("release_date") or ""
        year = release[:4] if len(release) >= 4 else None
        overview = r.get("overview") or ""
        candidates.append({
            "tmdb_id": r["id"],
            "title": r.get("title", ""),
            "year": year,
            "overview": overview[:150] + ("…" if len(overview) > 150 else ""),
            "poster_url": poster,
        })
    return candidates


def _tmdb_fetch_movie(tmdb_id: str, api_key: str) -> dict:
    return _tmdb_get(
        f"movie/{tmdb_id}",
        {"append_to_response": "credits", "language": "en-US"},
        api_key,
    )


def _parse_tmdb_movie(data: dict) -> dict:
    credits = data.get("credits", {})
    crew = credits.get("crew", [])
    cast = credits.get("cast", [])

    directors = [p["name"] for p in crew if p.get("job") == "Director"]
    writer_jobs = {"Screenplay", "Writer", "Story"}
    seen: set[str] = set()
    writers = [
        p["name"] for p in crew
        if p.get("department") == "Writing" and p.get("job") in writer_jobs
        and not (p["name"] in seen or seen.add(p["name"]))  # type: ignore[func-returns-value]
    ]

    creators: list[tuple[str, str]] = (
        [(name, "director") for name in directors]
        + [(name, "writer") for name in writers[:3]]
    )

    genres = [g["name"] for g in data.get("genres", [])]
    cast_names = [p["name"] for p in cast[:10]]

    release_date = data.get("release_date") or ""
    year = int(release_date[:4]) if len(release_date) >= 4 else None

    poster_path = data.get("poster_path")
    poster_url = f"{_TMDB_IMAGE_BASE}{poster_path}" if poster_path else None

    tmdb_id = data.get("id")
    imdb_id = data.get("imdb_id")
    external_ids: dict = {}
    if tmdb_id:
        external_ids["tmdb"] = str(tmdb_id)
    if imdb_id:
        external_ids["imdb"] = imdb_id

    extra: dict = {
        "runtime_minutes": data.get("runtime"),
        "genres": genres,
        "original_language": data.get("original_language"),
        "tagline": data.get("tagline") or None,
        "release_date": release_date or None,
        "cast": cast_names,
    }

    return {
        "title": data.get("title", "Unknown Title"),
        "subtitle": None,
        "authors": [name for name, _ in creators],
        "creator_role": "director",
        "creators": creators,
        "publisher": None,
        "publication_year": year,
        "description": data.get("overview") or None,
        "cover_image_url": poster_url,
        "isbn": None,
        "upc": None,
        "external_ids": external_ids,
        "extra_metadata": extra,
    }


def tmdb_search_title(query: str) -> list[dict]:
    """Search TMDb by title; returns a list of candidate dicts for the picker UI."""
    api_key = os.getenv("COMPENDIUM_TMDB_API_KEY")
    if not api_key:
        raise ExternalLookupError(
            "TMDb API key not configured. Set COMPENDIUM_TMDB_API_KEY to enable film metadata lookup."
        )
    return _tmdb_search_candidates(query, api_key)


class TMDbAdapter:
    def lookup(self, kind: str, value: str) -> dict | None:
        if kind != "tmdb_id":
            raise ExternalLookupError(f"TMDb adapter does not support identifier kind '{kind}'")
        api_key = os.getenv("COMPENDIUM_TMDB_API_KEY")
        if not api_key:
            raise ExternalLookupError(
                "TMDb API key not configured. Set COMPENDIUM_TMDB_API_KEY to enable film metadata lookup."
            )
        data = _tmdb_fetch_movie(value, api_key)
        if data.get("success") is False:
            return None
        return _parse_tmdb_movie(data)


# ---------------------------------------------------------------------------
# Registry and dispatcher
# ---------------------------------------------------------------------------

_tmdb_adapter = TMDbAdapter()

_ADAPTERS: dict[str, MetadataAdapter] = {
    "book": OpenLibraryAdapter(),
    "vinyl": MusicBrainzAdapter(),
    "cd": MusicBrainzAdapter(),
    "dvd": _tmdb_adapter,
    "bluray": _tmdb_adapter,
    "vhs": _tmdb_adapter,
}


def lookup_metadata(media_type_code: str, kind: str, value: str) -> dict | None:
    adapter = _ADAPTERS.get(media_type_code)
    if adapter is None:
        raise ExternalLookupError(
            f"No metadata adapter for media type '{media_type_code}'. "
            "Use manual entry for this type."
        )
    return adapter.lookup(kind, value)
