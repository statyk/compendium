"""Cover image proxy cache.

Fetches cover JPEGs from a fixed allowlist of external hosts (Open Library,
TMDb image CDN, Cover Art Archive, Archive.org and its CDN) and caches them
on disk so the browser always sees a same-origin resource — immune to
tracking-protection blocks, redirect-chain quirks, and upstream outages.

The module is deliberately framework-free so the web proxy route and the
``compendium maintenance prune-cover-cache`` CLI can share it.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

_ALLOWED_HOSTS: frozenset[str] = frozenset({
    "covers.openlibrary.org",
    "image.tmdb.org",
    "coverartarchive.org",
    "books.google.com",
    "covers.librarything.com",
})
_ALLOWED_SUFFIXES: tuple[str, ...] = (".archive.org",)

NEGATIVE_TTL_SECONDS: int = 24 * 3600
FETCH_TIMEOUT_SECONDS: float = 10.0


class CoverNotFound(Exception):
    """Upstream returned 4xx, a non-image, or a network error."""


class DisallowedHost(ValueError):
    """The URL (or a redirect target) is not on the allowlist."""


def cache_dir() -> Path:
    """Resolve the cache directory, creating it if needed.

    Honors ``COMPENDIUM_COVER_CACHE_DIR`` then ``XDG_CACHE_HOME``,
    falling back to ``~/.cache/compendium/covers``.
    """
    override = os.environ.get("COMPENDIUM_COVER_CACHE_DIR")
    if override:
        base = Path(override)
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        root = Path(xdg) if xdg else Path.home() / ".cache"
        base = root / "compendium" / "covers"
    base.mkdir(parents=True, exist_ok=True)
    return base


def host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in _ALLOWED_HOSTS:
        return True
    for suffix in _ALLOWED_SUFFIXES:
        bare = suffix.lstrip(".")
        if host == bare or host.endswith(suffix):
            return True
    return False


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def fetch_or_404(url: str) -> Path:
    """Return the cached JPEG path for ``url``, fetching on miss.

    - hit: returns the existing file path immediately
    - negative cache hit (<24h old): raises :class:`CoverNotFound`
    - fetch: follows redirects server-side, validates every hop against
      the allowlist, writes the JPEG via tmp+rename, returns its path
    - upstream error / non-image / 4xx: touches the negative-cache
      sentinel and raises :class:`CoverNotFound`

    Raises :class:`DisallowedHost` if the initial URL or any redirect
    target is not on the allowlist.
    """
    if not url.startswith(("http://", "https://")):
        raise DisallowedHost("url must be http(s)")
    if not host_allowed(url):
        raise DisallowedHost(f"host not on allowlist: {urlparse(url).hostname!r}")

    d = cache_dir()
    key = cache_key(url)
    hit = d / f"{key}.jpg"
    miss = d / f"{key}.404"

    if hit.exists():
        return hit
    if miss.exists() and (time.time() - miss.stat().st_mtime) < NEGATIVE_TTL_SECONDS:
        raise CoverNotFound("recently-failed cover, not retrying yet")

    try:
        resp = httpx.get(url, follow_redirects=True, timeout=FETCH_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        miss.touch()
        raise CoverNotFound("upstream fetch failed") from exc

    for hop in list(resp.history) + [resp]:
        if not host_allowed(str(hop.url)):
            raise DisallowedHost(f"redirect to disallowed host: {hop.url}")

    content_type = resp.headers.get("content-type", "").lower()
    if resp.status_code >= 400 or not content_type.startswith("image/"):
        miss.touch()
        raise CoverNotFound(f"upstream {resp.status_code} / {content_type!r}")

    tmp = d / f"{key}.tmp"
    tmp.write_bytes(resp.content)
    tmp.replace(hit)
    return hit


def invalidate(url: str) -> bool:
    """Remove the cached JPEG and any negative-cache sentinel for ``url``.

    Used when refreshing metadata: even if the upstream URL is unchanged,
    the bytes at that URL may have been updated. Forces the next read to
    re-fetch from upstream. Safe to call when nothing is cached.

    Returns True if at least one file was removed.
    """
    d = cache_dir()
    key = cache_key(url)
    removed = False
    for suffix in (".jpg", ".404"):
        path = d / f"{key}{suffix}"
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return removed


def prune(max_bytes: int) -> tuple[int, int]:
    """Evict cache files (oldest mtime first) until total size ≤ ``max_bytes``.

    Returns ``(removed_count, freed_bytes)``.
    """
    d = cache_dir()
    entries = []
    total = 0
    for p in d.iterdir():
        if not p.is_file():
            continue
        st = p.stat()
        entries.append((st.st_mtime, st.st_size, p))
        total += st.st_size

    if total <= max_bytes:
        return 0, 0

    entries.sort(key=lambda e: e[0])  # oldest first
    removed = 0
    freed = 0
    for _mtime, size, path in entries:
        if total <= max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        freed += size
        removed += 1
    return removed, freed
