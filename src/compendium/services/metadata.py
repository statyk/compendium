import os
import re
import time
import defusedxml.ElementTree as ET
from contextvars import ContextVar
from typing import Protocol, runtime_checkable

import httpx

from compendium.domain.errors import ExternalLookupError, GoogleBooksQuotaExhausted, ValidationError

_OPENLIBRARY_URL = "https://openlibrary.org/api/books"
_OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
_OPENLIBRARY_COVER_URL = "https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
_LOC_LCCN_URL = "https://lccn.loc.gov/{lccn}/marcxml"
_LOC_SRU_URL = "https://lx2.loc.gov/sru/catalog"
_MARC_NS = "http://www.loc.gov/MARC21/slim"
_MB_BASE = "https://musicbrainz.org/ws/2"
_MB_UA = "Compendium/0.1.0 (open-source library catalog)"
_TMDB_BASE = "https://api.themoviedb.org/3"
_TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
_GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"


# ---------------------------------------------------------------------------
# Identifier normalisation
# ---------------------------------------------------------------------------

def normalize_isbn(raw: str) -> str:
    isbn = re.sub(r"[\s\-]", "", raw)
    if len(isbn) == 10 and isbn[:9].isdigit() and (isbn[9].isdigit() or isbn[9] in "Xx"):
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
# Google Books adapter (books — primary when API key is configured)
# ---------------------------------------------------------------------------

def lookup_google_books(isbn: str, api_key: str) -> dict | None:
    """Fetch a book record from the Google Books volumes API.

    Returns a ``{id, volumeInfo}`` dict on hit, ``None`` on definitive not-found.
    Raises ``GoogleBooksQuotaExhausted`` on HTTP 403 dailyLimitExceeded.
    Raises ``ExternalLookupError`` on transport failure.
    Retries once after 1 s on HTTP 429 userRateLimitExceeded (burst limit).
    """
    params = {
        "q": f"isbn:{isbn}",
        "key": api_key,
        "fields": "items(id,volumeInfo(title,subtitle,authors,publisher,publishedDate,description,imageLinks,industryIdentifiers))",
    }
    for attempt in range(2):
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(_GOOGLE_BOOKS_URL, params=params)
        except httpx.HTTPError as exc:
            raise ExternalLookupError(f"Google Books request failed: {exc}") from exc

        if resp.status_code == 200:
            items = resp.json().get("items", [])
            return items[0] if items else None

        if resp.status_code == 429:
            # Per-second burst limit — retry once after a pause.
            if attempt == 0:
                time.sleep(1)
                continue
            raise ExternalLookupError("Google Books per-second rate limit exceeded after retry.")

        if resp.status_code == 403:
            try:
                reasons = [e.get("reason") for e in resp.json().get("error", {}).get("errors", [])]
            except Exception:
                reasons = []
            if "dailyLimitExceeded" in reasons or "userRateLimitExceeded" in reasons:
                raise GoogleBooksQuotaExhausted("Google Books daily quota exhausted.")
            raise ExternalLookupError(f"Google Books returned 403: {resp.text[:200]}")

        # Any other non-200 is treated as a transient/unknown error.
        raise ExternalLookupError(f"Google Books returned HTTP {resp.status_code}")

    raise ExternalLookupError("Google Books request failed after retry.")  # unreachable


def parse_google_books(volume: dict, isbn: str) -> dict:
    """Map a Google Books volume ``{id, volumeInfo}`` to the canonical metadata dict."""
    info = volume.get("volumeInfo", {})
    volume_id: str = volume.get("id", "")

    authors: list[str] = info.get("authors") or []

    pub_date: str = info.get("publishedDate", "")
    year: int | None = None
    m = re.search(r"\d{4}", pub_date)
    if m:
        year = int(m.group())

    links = info.get("imageLinks") or {}
    raw_url = (
        links.get("large") or links.get("medium")
        or links.get("small") or links.get("thumbnail")
    )
    cover_url: str | None = raw_url.replace("http://", "https://") if raw_url else None

    return {
        "title": info.get("title") or "Unknown Title",
        "subtitle": info.get("subtitle"),
        "authors": authors,
        "creator_role": "author",
        "publisher": info.get("publisher"),
        "publication_year": year,
        "description": info.get("description"),
        "cover_image_url": cover_url,
        "isbn": isbn,
        "upc": None,
        "external_ids": {"google_books": volume_id} if volume_id else {},
        "extra_metadata": {},
        "lc_classification": None,
        "ddc_classification": None,
        "lccn": None,
    }


class GoogleBooksAdapter:
    def lookup(self, kind: str, value: str) -> dict | None:
        if kind != "isbn":
            raise ExternalLookupError(f"Google Books does not support identifier kind '{kind}'")
        from compendium.services.site_settings import get_site_setting

        api_key = get_site_setting("google_books_api_key")
        if not api_key:
            return None
        try:
            volume = lookup_google_books(value, api_key)
        except GoogleBooksQuotaExhausted:
            _mark_gb_quota_exhausted()
            return None
        if volume is None:
            return None
        return parse_google_books(volume, value)


# ---------------------------------------------------------------------------
# Google Books quota circuit breaker
# ---------------------------------------------------------------------------

# Set by lookup_metadata for the duration of one call so that the quota READ
# check inside _resolve_book_adapter can reuse the already-open session.
_active_lookup_session: ContextVar = ContextVar("_active_lookup_session", default=None)

_GB_QUOTA_ADAPTER = "GoogleBooksAdapter"
_GB_QUOTA_KIND = "_quota"
_GB_QUOTA_VALUE = "exhausted"


def _mark_gb_quota_exhausted(*, session=None) -> None:
    """Persist the quota-exhausted sentinel row so all processes see it.

    ``session`` is accepted for callers that manage their own transaction but
    note it must not be a session that may roll back (e.g. a dry-run import
    session) — the sentinel must survive any outer rollback.  When omitted the
    function opens its own short-lived session via
    ``compendium.db.session.session_scope``.
    """
    import logging
    from datetime import datetime, timezone

    from compendium.domain.models import MetadataCache

    logger = logging.getLogger(__name__)

    def _write(s) -> None:
        entry = s.get(MetadataCache, (_GB_QUOTA_ADAPTER, _GB_QUOTA_KIND, _GB_QUOTA_VALUE))
        if entry is None:
            entry = MetadataCache(
                adapter=_GB_QUOTA_ADAPTER,
                kind=_GB_QUOTA_KIND,
                lookup_value=_GB_QUOTA_VALUE,
            )
            s.add(entry)
        entry.is_negative = True
        entry.payload = None
        entry.fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)

    try:
        if session is not None:
            _write(session)
        else:
            from compendium.db.session import session_scope
            with session_scope() as s:
                _write(s)
        logger.warning(
            "Google Books daily quota exhausted. Using Open Library fallback "
            "for book lookups. Quota typically resets after 24 h."
        )
    except Exception:
        logger.exception("Failed to persist Google Books quota sentinel")


def is_gb_quota_exhausted(*, session=None) -> bool:
    """Return True if a valid quota-exhausted sentinel row exists (< 24 h old).

    When ``session`` is provided (or when called from inside ``lookup_metadata``
    where ``_active_lookup_session`` is set), the existing session is reused for
    the read — no new connection is opened.  When neither is available the
    function opens its own short-lived session via
    ``compendium.db.session.session_scope``.
    """
    from datetime import datetime, timedelta, timezone

    from compendium.domain.models import MetadataCache

    def _check(s) -> bool:
        entry = s.get(MetadataCache, (_GB_QUOTA_ADAPTER, _GB_QUOTA_KIND, _GB_QUOTA_VALUE))
        if entry is None:
            return False
        threshold = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
        return entry.fetched_at >= threshold

    s = session or _active_lookup_session.get()
    try:
        if s is not None:
            return _check(s)
        from compendium.db.session import session_scope
        with session_scope() as s2:
            return _check(s2)
    except Exception:
        return False


def clear_gb_quota_exhausted(*, session=None) -> bool:
    """Delete the quota-exhausted sentinel row. Returns True if one existed."""
    from compendium.domain.models import MetadataCache

    def _clear(s) -> bool:
        entry = s.get(MetadataCache, (_GB_QUOTA_ADAPTER, _GB_QUOTA_KIND, _GB_QUOTA_VALUE))
        if entry is None:
            return False
        s.delete(entry)
        return True

    if session is not None:
        return _clear(session)
    from compendium.db.session import session_scope
    with session_scope() as s:
        return _clear(s)


def open_library_search_title(query: str) -> list[dict]:
    """Search Open Library by title; returns candidate dicts for the picker UI.

    Only includes results with at least one ISBN, because the downstream add-by-ISBN
    path is the only way to populate a Work from an Open Library pick today.
    """
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                _OPENLIBRARY_SEARCH_URL,
                params={
                    "title": query,
                    "limit": "15",
                    # OL's default response omits the isbn field; request it explicitly.
                    "fields": "title,author_name,first_publish_year,cover_i,isbn",
                },
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExternalLookupError(f"Open Library request failed: {exc}") from exc

    candidates: list[dict] = []
    for doc in resp.json().get("docs", []):
        isbns = doc.get("isbn") or []
        isbn13 = next((i for i in isbns if len(i) == 13 and i.isdigit()), None)
        isbn = isbn13 or next((i for i in isbns if i), None)
        if not isbn:
            continue
        authors = doc.get("author_name") or []
        secondary = "by " + ", ".join(authors[:2]) if authors else ""
        year = str(doc["first_publish_year"]) if doc.get("first_publish_year") else None
        cover_id = doc.get("cover_i")
        image_url = _OPENLIBRARY_COVER_URL.format(cover_id=cover_id) if cover_id else None
        candidates.append({
            "identifier_value": isbn,
            "title": doc.get("title", ""),
            "year": year,
            "secondary": secondary,
            "tertiary": f"ISBN {isbn}",
            "image_url": image_url,
        })
        if len(candidates) >= 10:
            break
    return candidates


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


def lookup_cover_from_google_books(isbn: str, *, api_key: str | None) -> str | None:
    """Fetch a cover thumbnail URL from the Google Books API.

    Returns the largest available thumbnail URL, or None on any failure.
    Requires COMPENDIUM_GOOGLE_BOOKS_API_KEY.
    """
    if not api_key or not isbn:
        return None
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                _GOOGLE_BOOKS_URL,
                params={"q": f"isbn:{isbn}", "key": api_key, "fields": "items/volumeInfo/imageLinks"},
            )
            if resp.status_code != 200:
                return None
        items = resp.json().get("items", [])
        if not items:
            return None
        links = items[0].get("volumeInfo", {}).get("imageLinks", {})
        url = links.get("large") or links.get("medium") or links.get("small") or links.get("thumbnail")
        return url or None
    except Exception:
        return None


def _lookup_ol_cover_by_isbn(isbn: str) -> str | None:
    """Probe Open Library's covers-by-ISBN endpoint; return URL or None.

    Uses ``?default=false`` so a 404 means no cover exists (instead of a
    placeholder image). Safe to call without an API key.
    """
    url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.head(url, params={"default": "false"}, follow_redirects=True)
        return url if resp.status_code == 200 else None
    except Exception:
        return None


def lookup_cover_fallbacks(
    isbn: str,
    *,
    google_books_key: str | None,
    primary: str = "openlibrary",
    bypass_cache: bool = False,
    session=None,
) -> str | None:
    """Try the *other* source's cover when the primary returned no cover URL.

    When ``primary`` is ``'openlibrary'`` (default), falls back to Google Books.
    When ``primary`` is ``'googlebooks'``, falls back to Open Library
    covers-by-ISBN (no API key required).
    """
    from compendium.services.metadata_cache import get_or_fetch

    if primary == "googlebooks":
        # Primary was GB → try Open Library covers.
        if session is None:
            return _lookup_ol_cover_by_isbn(isbn)
        return get_or_fetch(
            session,
            "ol_cover",
            "isbn",
            isbn,
            lambda: _lookup_ol_cover_by_isbn(isbn),
            bypass_cache=bypass_cache,
        )

    # Primary was OL → try Google Books (original behavior).
    if session is None:
        return lookup_cover_from_google_books(isbn, api_key=google_books_key)
    return get_or_fetch(
        session,
        "gb_cover",
        "isbn",
        isbn,
        lambda: lookup_cover_from_google_books(isbn, api_key=google_books_key),
        bypass_cache=bypass_cache,
    )


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


_MB_FORMAT_KEYWORDS: dict[str, str] = {
    # media_type -> substring that must appear in the release's first-medium format
    "vinyl": "vinyl",
    "cd": "cd",
}

# Articles/conjunctions that users routinely omit when searching. Stripping them
# lets "Dark Side of the Moon" match "The Dark Side of the Moon".
_MB_STOPWORDS = {"a", "an", "the", "of", "and"}


def _mb_build_release_query(query: str) -> str:
    """Build a MusicBrainz release-search Lucene query from free-form user input.

    Tokenises on alphanumerics (which also sidesteps Lucene escaping) and drops
    common articles so "Dark Side of the Moon" matches "The Dark Side of the
    Moon". Each remaining token must appear in either the release title or the
    artist, so mixed queries like "pink floyd dark side" also work.
    """
    tokens = [
        t for t in re.findall(r"[a-z0-9]+", query.lower()) if t not in _MB_STOPWORDS
    ]
    if not tokens:
        escaped = query.replace('"', '\\"')
        return f'release:"{escaped}"'
    return " AND ".join(f"(release:{t} OR artist:{t})" for t in tokens)


def musicbrainz_search_title(query: str, media_type: str | None = None) -> list[dict]:
    """Search MusicBrainz releases by title; returns candidate dicts for the picker UI.

    When ``media_type`` is ``vinyl`` or ``cd``, results are filtered client-side to
    releases whose first medium's format matches that type. Other media types (or
    ``None``) return unfiltered results.
    """
    keyword = _MB_FORMAT_KEYWORDS.get(media_type or "")
    # When filtering, widen the initial pull so the filter has enough to work with.
    limit = "50" if keyword else "15"
    data = _mb_get(
        "release",
        {"query": _mb_build_release_query(query), "fmt": "json", "limit": limit},
    )
    candidates: list[dict] = []
    for r in data.get("releases", []):
        mbid = r.get("id")
        if not mbid:
            continue
        media_list = r.get("media", []) or []
        fmt = media_list[0].get("format", "") if media_list else ""
        if keyword and keyword not in fmt.lower():
            continue
        artists = [
            ac["artist"]["name"]
            for ac in r.get("artist-credit", [])
            if isinstance(ac, dict) and "artist" in ac
        ]
        secondary = "by " + ", ".join(artists[:2]) if artists else ""
        date_str = r.get("date") or ""
        year = date_str[:4] if len(date_str) >= 4 and date_str[:4].isdigit() else None
        country = r.get("country") or ""
        tertiary_parts = [p for p in (country, fmt) if p]
        candidates.append({
            "identifier_value": mbid,
            "title": r.get("title", ""),
            "year": year,
            "secondary": secondary,
            "tertiary": " · ".join(tertiary_parts),
            "image_url": None,
        })
        if len(candidates) >= 10:
            break
    return candidates


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
        overview_trunc = overview[:150] + ("…" if len(overview) > 150 else "")
        candidates.append({
            "identifier_value": str(r["id"]),
            "title": r.get("title", ""),
            "year": year,
            "secondary": overview_trunc,
            "tertiary": "",
            "image_url": poster,
            # Backwards-compat keys (used by CLI picker and any callers predating
            # the unified title-candidate shape).
            "tmdb_id": r["id"],
            "overview": overview_trunc,
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
    from compendium.services.site_settings import get_site_setting

    api_key = get_site_setting("tmdb_api_key")
    if not api_key:
        raise ExternalLookupError(
            "TMDb API key not configured. Set COMPENDIUM_TMDB_API_KEY or add it "
            "via Admin → System → Secrets to enable film metadata lookup."
        )
    return _tmdb_search_candidates(query, api_key)


class TMDbAdapter:
    def lookup(self, kind: str, value: str) -> dict | None:
        if kind != "tmdb_id":
            raise ExternalLookupError(f"TMDb adapter does not support identifier kind '{kind}'")
        from compendium.services.site_settings import get_site_setting

        api_key = get_site_setting("tmdb_api_key")
        if not api_key:
            raise ExternalLookupError(
                "TMDb API key not configured. Set COMPENDIUM_TMDB_API_KEY or add it "
                "via Admin → System → Secrets to enable film metadata lookup."
            )
        data = _tmdb_fetch_movie(value, api_key)
        if data.get("success") is False:
            return None
        return _parse_tmdb_movie(data)


# ---------------------------------------------------------------------------
# Registry and dispatcher
# ---------------------------------------------------------------------------

_tmdb_adapter = TMDbAdapter()
_GB_ADAPTER = GoogleBooksAdapter()
_OL_ADAPTER = OpenLibraryAdapter()

_ADAPTERS: dict[str, MetadataAdapter] = {
    "vinyl": MusicBrainzAdapter(),
    "cd": MusicBrainzAdapter(),
    "dvd": _tmdb_adapter,
    "bluray": _tmdb_adapter,
    "vhs": _tmdb_adapter,
}


def _resolve_book_adapter() -> MetadataAdapter:
    """Return the active primary book adapter based on preference + key + quota."""
    from compendium.services.site_settings import get_site_setting

    pref = get_site_setting("book_metadata_source_preference") or "googlebooks"
    if pref != "googlebooks":
        return _OL_ADAPTER
    if not get_site_setting("google_books_api_key"):
        return _OL_ADAPTER
    if is_gb_quota_exhausted():
        return _OL_ADAPTER
    return _GB_ADAPTER


def _resolve_book_chain() -> list[MetadataAdapter]:
    """Return the ordered list of book adapters to try (primary first).

    Secondary availability uses the same gating as primary: GB requires a key
    AND an un-exhausted quota. OL is always available. Fallback is skipped
    entirely when ``book_metadata_fallback_enabled`` is false.

    Must be called AFTER ``_active_lookup_session`` is set so the quota check
    inside ``is_gb_quota_exhausted`` piggybacks on the caller's session.
    """
    from compendium.services.site_settings import get_site_setting

    primary = _resolve_book_adapter()
    fallback_enabled = get_site_setting("book_metadata_fallback_enabled")
    if fallback_enabled is None:
        fallback_enabled = True

    if not fallback_enabled:
        return [primary]

    if primary is _GB_ADAPTER:
        # OL is always available as secondary.
        return [_GB_ADAPTER, _OL_ADAPTER]

    # primary is OL; GB is secondary only if key present and quota not exhausted.
    gb_key = get_site_setting("google_books_api_key")
    if gb_key and not is_gb_quota_exhausted():
        return [_OL_ADAPTER, _GB_ADAPTER]
    return [_OL_ADAPTER]


def get_book_primary_adapter_name() -> str:
    """Return 'googlebooks' or 'openlibrary' based on current runtime config."""
    return "googlebooks" if _resolve_book_adapter() is _GB_ADAPTER else "openlibrary"


def _get_adapter(media_type_code: str) -> MetadataAdapter:
    if media_type_code == "book":
        return _resolve_book_adapter()
    adapter = _ADAPTERS.get(media_type_code)
    if adapter is None:
        raise ExternalLookupError(
            f"No metadata adapter for media type '{media_type_code}'. "
            "Use manual entry for this type."
        )
    return adapter


def _adapter_source_name(adapter: "MetadataAdapter") -> str | None:
    """Return the canonical source-name string for a known adapter, or None."""
    if adapter is _GB_ADAPTER:
        return "googlebooks"
    if adapter is _OL_ADAPTER:
        return "openlibrary"
    return None


def _adapter_for_source(source: str) -> MetadataAdapter:
    """Return the adapter singleton for a named source string."""
    if source == "googlebooks":
        return _GB_ADAPTER
    if source == "openlibrary":
        return _OL_ADAPTER
    if source == "musicbrainz":
        return _ADAPTERS["vinyl"]
    if source == "tmdb":
        return _tmdb_adapter
    from compendium.domain.errors import ExternalLookupError as _ELE
    raise _ELE(f"Unknown metadata source: '{source}'")


# Valid source names per media type — used by lookup_metadata_from_source.
_VALID_SOURCES_FOR_MEDIA: dict[str, frozenset] = {
    "book": frozenset({"googlebooks", "openlibrary"}),
    "vinyl": frozenset({"musicbrainz"}),
    "cd": frozenset({"musicbrainz"}),
    "dvd": frozenset({"tmdb"}),
    "bluray": frozenset({"tmdb"}),
    "vhs": frozenset({"tmdb"}),
}


# ---------------------------------------------------------------------------
# API-key validation
# ---------------------------------------------------------------------------

class KeyValidationResult:
    """Result of validate_google_books_key."""

    __slots__ = ("ok", "status_code", "reason", "warning")

    def __init__(
        self,
        ok: bool,
        *,
        status_code: int | None = None,
        reason: str | None = None,
        warning: str | None = None,
    ) -> None:
        self.ok = ok
        self.status_code = status_code
        self.reason = reason
        self.warning = warning


def validate_google_books_key(
    key: str, *, sample_isbn: str = "9780441013593"
) -> KeyValidationResult:
    """Test a Google Books API key with a live lookup against a known ISBN.

    Returns ``ok=True`` if the key is accepted. Quota-exhausted (daily limit)
    also returns ``ok=True`` with a ``warning`` — the key is valid, just
    temporarily blocked. All other errors return ``ok=False`` with ``reason``.
    """
    try:
        lookup_google_books(sample_isbn, key)
        return KeyValidationResult(ok=True)
    except GoogleBooksQuotaExhausted:
        return KeyValidationResult(
            ok=True,
            warning=(
                "Google Books daily quota is currently exhausted. The key is valid "
                "but lookups will fail until it resets (typically 24 hours)."
            ),
        )
    except ExternalLookupError as exc:
        return KeyValidationResult(ok=False, reason=str(exc))


# ---------------------------------------------------------------------------
# Per-source forced lookup
# ---------------------------------------------------------------------------

def lookup_metadata_from_source(
    media_type_code: str,
    kind: str,
    value: str,
    source: str,
    *,
    session=None,
    bypass_cache: bool = True,
) -> tuple[dict | None, str | None]:
    """Fetch metadata from a specific named source, bypassing the primary/fallback resolver.

    ``bypass_cache`` defaults to True because callers typically choose a source
    to override a cached result they distrust.

    Raises ``ValueError`` if *source* is not valid for *media_type_code*.
    Raises ``ExternalLookupError`` on transport failure (not swallowed here —
    the caller decides how to present the error).
    """
    valid = _VALID_SOURCES_FOR_MEDIA.get(media_type_code)
    if valid is None:
        raise ValueError(
            f"No external metadata adapters for media type '{media_type_code}'"
        )
    if source not in valid:
        raise ValueError(
            f"Source '{source}' is not available for media type '{media_type_code}'. "
            f"Valid sources: {sorted(valid)}"
        )

    adapter = _adapter_for_source(source)

    if session is None:
        result = adapter.lookup(kind, value)
        return (result, source) if result is not None else (None, None)

    from compendium.services.metadata_cache import get_or_fetch

    result = get_or_fetch(
        session,
        type(adapter).__name__,
        kind,
        value,
        lambda: adapter.lookup(kind, value),
        bypass_cache=bypass_cache,
    )
    return (result, source) if result is not None else (None, None)


def lookup_metadata_with_source(
    media_type_code: str,
    kind: str,
    value: str,
    *,
    bypass_cache: bool = False,
    session=None,
    write_buffer=None,
) -> tuple[dict | None, str | None]:
    """Like ``lookup_metadata`` but also returns the name of the adapter that
    produced the non-None result ('googlebooks', 'openlibrary', etc.), or None
    when nothing was found.

    For books, tries adapters in the order returned by ``_resolve_book_chain``
    (primary, then optional secondary when ``book_metadata_fallback_enabled``
    is true and a secondary is available). Google Books transport errors
    (``ExternalLookupError``) are swallowed regardless of whether GB is primary
    or secondary — a transient error should not abort an OL fallback. Non-GB
    adapter errors propagate so callers can detect genuine failures."""
    # Make the session visible to is_gb_quota_exhausted (called from the chain
    # resolver) so it can piggyback on the open connection instead of opening a
    # duplicate one.  _mark_gb_quota_exhausted intentionally does NOT use this —
    # it opens its own short-lived session so the sentinel survives outer rollback.
    _tok = _active_lookup_session.set(session) if session is not None else None
    try:
        if media_type_code == "book":
            # Chain must be computed after ContextVar is set (quota check uses it).
            chain = _resolve_book_chain()
        else:
            chain = [_get_adapter(media_type_code)]

        if session is None:
            for adapter in chain:
                is_gb = adapter is _GB_ADAPTER
                try:
                    result = adapter.lookup(kind, value)
                except ExternalLookupError:
                    if is_gb:
                        result = None
                    else:
                        raise
                if result is not None:
                    return result, _adapter_source_name(adapter)
            return None, None

        from compendium.services.metadata_cache import get_or_fetch

        for adapter in chain:
            is_gb = adapter is _GB_ADAPTER
            adapter_name = type(adapter).__name__
            try:
                result = get_or_fetch(
                    session,
                    adapter_name,
                    kind,
                    value,
                    lambda: adapter.lookup(kind, value),
                    bypass_cache=bypass_cache,
                    write_buffer=write_buffer,
                )
            except ExternalLookupError:
                if is_gb:
                    result = None
                else:
                    raise
            if result is not None:
                return result, _adapter_source_name(adapter)
        return None, None
    finally:
        if _tok is not None:
            _active_lookup_session.reset(_tok)


def lookup_metadata(
    media_type_code: str,
    kind: str,
    value: str,
    *,
    bypass_cache: bool = False,
    session=None,
    write_buffer=None,
) -> dict | None:
    result, _ = lookup_metadata_with_source(
        media_type_code, kind, value,
        bypass_cache=bypass_cache,
        session=session,
        write_buffer=write_buffer,
    )
    return result
