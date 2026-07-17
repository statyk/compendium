"""Persistent cache for external metadata lookups.

Keyed on (adapter, kind, lookup_value); payload is JSON; NULL for negative entries.

WRITE BUFFER PATTERN
--------------------
The import service runs inside an outer session that rolls back on dry-run.
Opening a *new* session_scope() inside the import loop would conflict on
SQLite (write-lock) and on StaticPool test fixtures (same connection — the
new session's commit would flush the import's uncommitted state).

Solution: callers pass a WriteBuffer. Writes are collected in-process and
flushed after the outer session is committed or rolled back. Reads always
go through the caller's existing session so they see its uncommitted state.

For non-import call sites (add_from_lookup, refresh_metadata) no write buffer
is needed — writes go directly to the caller's session.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, TypeVar

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from compendium.domain.errors import ExternalLookupError
from compendium.domain.models import MetadataCache

log = logging.getLogger(__name__)

T = TypeVar("T")

_NEGATIVE_TTL_HOURS: int = 24
_POSITIVE_TTL_DAYS_DEFAULT: int = 30


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _positive_ttl_days() -> int:
    try:
        from compendium.services.site_settings import get_site_setting

        v = get_site_setting("metadata_cache_ttl_days")
        return int(v) if v is not None else _POSITIVE_TTL_DAYS_DEFAULT
    except Exception:
        return _POSITIVE_TTL_DAYS_DEFAULT


# ---------------------------------------------------------------------------
# WriteBuffer — deferred writes for the import path
# ---------------------------------------------------------------------------


@dataclass
class WriteBuffer:
    """Accumulates MetadataCache rows to write after the import session settles.

    ``session_factory`` is a callable that returns a context manager yielding a
    Session. Defaults to ``session_scope()`` (the app's configured DB). Tests
    inject a factory bound to the test engine so flush() targets the same DB
    that the test can query.
    """

    _entries: list[MetadataCache] = field(default_factory=list)
    session_factory: object = None  # Callable[[], ContextManager[Session]] | None

    def add(self, entry: MetadataCache) -> None:
        existing = next(
            (
                e
                for e in self._entries
                if e.adapter == entry.adapter
                and e.kind == entry.kind
                and e.lookup_value == entry.lookup_value
            ),
            None,
        )
        if existing is not None:
            self._entries.remove(existing)
        self._entries.append(entry)

    def flush(self) -> None:
        if not self._entries:
            return

        if self.session_factory is not None:
            ctx = self.session_factory()
        else:
            from compendium.db.session import session_scope
            ctx = session_scope()

        n = len(self._entries)
        with ctx as session:
            for entry in self._entries:
                existing = session.get(
                    MetadataCache, (entry.adapter, entry.kind, entry.lookup_value)
                )
                if existing is not None:
                    existing.payload = entry.payload
                    existing.is_negative = entry.is_negative
                    existing.fetched_at = entry.fetched_at
                else:
                    session.add(entry)
        self._entries.clear()
        log.debug("metadata_cache: flushed %d entries", n)


# ---------------------------------------------------------------------------
# Core cache function
# ---------------------------------------------------------------------------


def get_or_fetch(
    session: Session,
    adapter: str,
    kind: str,
    value: str,
    fetcher: Callable[[], T],
    *,
    bypass_cache: bool = False,
    write_buffer: WriteBuffer | None = None,
) -> T:
    """Return a cached result or call fetcher and cache the response.

    - Cache hit (within TTL): return cached value (None for negative entries).
    - Cache miss or expired: call fetcher, cache result, return it.
    - fetcher raises ExternalLookupError: propagate without caching.
    - bypass_cache=True: skip the read check; always call fetcher and update cache.
    """
    now = _now_utc()

    if not bypass_cache:
        row = _get_row(session, adapter, kind, value)
        if row is not None:
            if row.is_negative:
                cutoff = now - timedelta(hours=_NEGATIVE_TTL_HOURS)
            else:
                cutoff = now - timedelta(days=_positive_ttl_days())

            fetched = row.fetched_at
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)

            if fetched > cutoff:
                log.debug(
                    "metadata_cache: HIT %s/%s/%s (negative=%s)",
                    adapter, kind, value, row.is_negative,
                )
                if row.is_negative:
                    return None  # type: ignore[return-value]
                return json.loads(row.payload)

    # Cache miss, expired, or bypassed — call the network.
    try:
        result = fetcher()
    except ExternalLookupError:
        raise

    is_negative = result is None
    payload_json = None if is_negative else json.dumps(result)

    entry = MetadataCache(
        adapter=adapter,
        kind=kind,
        lookup_value=value,
        payload=payload_json,
        is_negative=is_negative,
        fetched_at=now,
    )

    if write_buffer is not None:
        write_buffer.add(entry)
        log.debug(
            "metadata_cache: MISS %s/%s/%s → buffered (negative=%s)",
            adapter, kind, value, is_negative,
        )
    else:
        _upsert_to_session(session, entry)
        log.debug(
            "metadata_cache: MISS %s/%s/%s → written (negative=%s)",
            adapter, kind, value, is_negative,
        )

    return result  # type: ignore[return-value]


def _get_row(
    session: Session, adapter: str, kind: str, value: str
) -> MetadataCache | None:
    """Look up a cache row, including one still *pending* in the session.

    A row session.add()-ed earlier in the same session is pending — not in
    the identity map and not yet in the DB — so session.get() misses it
    unless autoflush flushes first. With autoflush=False sessions (the app
    default), that miss caused a second same-key lookup to add a duplicate
    row and violate the primary key at commit.
    """
    row = session.get(MetadataCache, (adapter, kind, value))
    if row is not None:
        return row
    for obj in session.new:
        if (
            isinstance(obj, MetadataCache)
            and obj.adapter == adapter
            and obj.kind == kind
            and obj.lookup_value == value
        ):
            return obj
    return None


def _upsert_to_session(session: Session, entry: MetadataCache) -> None:
    existing = _get_row(session, entry.adapter, entry.kind, entry.lookup_value)
    if existing is not None:
        existing.payload = entry.payload
        existing.is_negative = entry.is_negative
        existing.fetched_at = entry.fetched_at
    else:
        session.add(entry)


# ---------------------------------------------------------------------------
# Maintenance helpers
# ---------------------------------------------------------------------------


def prune_expired(session: Session, *, dry_run: bool = False) -> int:
    """Delete rows past their TTL. Returns number of (would-be) deleted rows.

    ``dry_run=True`` counts the matching rows and returns without deleting.
    """
    now = _now_utc()
    positive_cutoff = now - timedelta(days=_positive_ttl_days())
    negative_cutoff = now - timedelta(hours=_NEGATIVE_TTL_HOURS)

    where_clause = (
        (MetadataCache.is_negative == False)  # noqa: E712
        & (MetadataCache.fetched_at < positive_cutoff)
    ) | (
        (MetadataCache.is_negative == True)  # noqa: E712
        & (MetadataCache.fetched_at < negative_cutoff)
    )

    if dry_run:
        return session.query(MetadataCache).filter(where_clause).count()

    result = session.execute(delete(MetadataCache).where(where_clause))
    return result.rowcount


def clear_all(session: Session) -> int:
    """Delete all rows. Returns number of deleted rows."""
    result = session.execute(delete(MetadataCache))
    return result.rowcount


@dataclass
class CacheStats:
    total: int
    positive: int
    negative: int
    expired_positive: int
    expired_negative: int
    oldest_fetched_at: datetime | None
    adapter_counts: dict[str, int]


def get_stats(session: Session) -> CacheStats:
    """Return cache row counts and metadata."""
    now = _now_utc()
    positive_cutoff = now - timedelta(days=_positive_ttl_days())
    negative_cutoff = now - timedelta(hours=_NEGATIVE_TTL_HOURS)

    total = session.scalar(select(func.count()).select_from(MetadataCache)) or 0
    positive = (
        session.scalar(
            select(func.count()).select_from(MetadataCache).where(
                MetadataCache.is_negative == False  # noqa: E712
            )
        )
        or 0
    )
    negative = total - positive

    expired_positive = (
        session.scalar(
            select(func.count()).select_from(MetadataCache).where(
                (MetadataCache.is_negative == False)  # noqa: E712
                & (MetadataCache.fetched_at < positive_cutoff)
            )
        )
        or 0
    )
    expired_negative = (
        session.scalar(
            select(func.count()).select_from(MetadataCache).where(
                (MetadataCache.is_negative == True)  # noqa: E712
                & (MetadataCache.fetched_at < negative_cutoff)
            )
        )
        or 0
    )

    oldest_row = session.scalar(
        select(func.min(MetadataCache.fetched_at))
    )

    rows = session.execute(
        select(MetadataCache.adapter, func.count().label("cnt"))
        .group_by(MetadataCache.adapter)
        .order_by(func.count().desc())
    ).all()
    adapter_counts = {r.adapter: r.cnt for r in rows}

    return CacheStats(
        total=total,
        positive=positive,
        negative=negative,
        expired_positive=expired_positive,
        expired_negative=expired_negative,
        oldest_fetched_at=oldest_row,
        adapter_counts=adapter_counts,
    )
