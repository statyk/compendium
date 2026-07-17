"""Unit tests for the metadata_cache service module.

No DB required — the cache layer is tested against a SQLite in-memory session
via a lightweight fixture (same pattern as other unit tests in this project).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from compendium.domain.errors import ExternalLookupError
from compendium.domain.models import Base, MetadataCache
from compendium.services.metadata_cache import (
    WriteBuffer,
    _NEGATIVE_TTL_HOURS,
    clear_all,
    get_or_fetch,
    get_stats,
    prune_expired,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_row(
    session: Session,
    *,
    adapter: str = "OpenLibraryAdapter",
    kind: str = "isbn",
    value: str = "9780441013593",
    payload: dict | None = None,
    is_negative: bool = False,
    age_hours: float = 0.0,
) -> MetadataCache:
    fetched_at = _now() - timedelta(hours=age_hours)
    row = MetadataCache(
        adapter=adapter,
        kind=kind,
        lookup_value=value,
        payload=json.dumps(payload) if payload is not None else None,
        is_negative=is_negative,
        fetched_at=fetched_at,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Cache miss → fetcher called, row written
# ---------------------------------------------------------------------------


def test_cache_miss_calls_fetcher_and_writes_row(db_session):
    payload = {"title": "Dune"}
    fetcher = MagicMock(return_value=payload)

    result = get_or_fetch(db_session, "OL", "isbn", "9780441013593", fetcher)

    fetcher.assert_called_once()
    assert result == payload

    row = db_session.get(MetadataCache, ("OL", "isbn", "9780441013593"))
    assert row is not None
    assert json.loads(row.payload) == payload
    assert row.is_negative is False


# ---------------------------------------------------------------------------
# Cache hit within TTL → fetcher NOT called
# ---------------------------------------------------------------------------


def test_cache_hit_within_ttl_skips_fetcher(db_session):
    _make_row(db_session, adapter="OL", value="9780441013593",
              payload={"title": "Dune"}, age_hours=1.0)

    fetcher = MagicMock()
    with patch("compendium.services.metadata_cache._positive_ttl_days", return_value=30):
        result = get_or_fetch(db_session, "OL", "isbn", "9780441013593", fetcher)

    fetcher.assert_not_called()
    assert result == {"title": "Dune"}


# ---------------------------------------------------------------------------
# Cache hit past TTL → fetcher called, row updated
# ---------------------------------------------------------------------------


def test_cache_hit_past_ttl_calls_fetcher_and_updates(db_session):
    _make_row(db_session, adapter="OL", value="9780441013593",
              payload={"title": "Dune"}, age_hours=24 * 31)  # >30 days

    new_payload = {"title": "Dune (updated)"}
    fetcher = MagicMock(return_value=new_payload)

    with patch("compendium.services.metadata_cache._positive_ttl_days", return_value=30):
        result = get_or_fetch(db_session, "OL", "isbn", "9780441013593", fetcher)

    fetcher.assert_called_once()
    assert result == new_payload

    row = db_session.get(MetadataCache, ("OL", "isbn", "9780441013593"))
    assert json.loads(row.payload) == new_payload


# ---------------------------------------------------------------------------
# Negative cache
# ---------------------------------------------------------------------------


def test_negative_cache_returns_none_without_fetcher(db_session):
    _make_row(db_session, adapter="OL", value="0000000000000",
              is_negative=True, age_hours=0.5)

    fetcher = MagicMock()
    result = get_or_fetch(db_session, "OL", "isbn", "0000000000000", fetcher)

    fetcher.assert_not_called()
    assert result is None


def test_negative_cache_past_ttl_refetches(db_session):
    _make_row(db_session, adapter="OL", value="0000000000000",
              is_negative=True, age_hours=_NEGATIVE_TTL_HOURS + 1)

    fetcher = MagicMock(return_value=None)
    result = get_or_fetch(db_session, "OL", "isbn", "0000000000000", fetcher)

    fetcher.assert_called_once()
    assert result is None


def test_fetcher_returns_none_writes_negative_row(db_session):
    fetcher = MagicMock(return_value=None)
    result = get_or_fetch(db_session, "OL", "isbn", "0000000000000", fetcher)

    assert result is None
    row = db_session.get(MetadataCache, ("OL", "isbn", "0000000000000"))
    assert row is not None
    assert row.is_negative is True
    assert row.payload is None


# ---------------------------------------------------------------------------
# Transport error — do NOT cache, re-raise
# ---------------------------------------------------------------------------


def test_external_lookup_error_not_cached(db_session):
    def boom():
        raise ExternalLookupError("network timeout")

    with pytest.raises(ExternalLookupError):
        get_or_fetch(db_session, "OL", "isbn", "9780441013593", boom)

    row = db_session.get(MetadataCache, ("OL", "isbn", "9780441013593"))
    assert row is None


# ---------------------------------------------------------------------------
# bypass_cache → fetcher always called; result still written
# ---------------------------------------------------------------------------


def test_bypass_cache_calls_fetcher_even_on_hit(db_session):
    _make_row(db_session, adapter="OL", value="9780441013593",
              payload={"title": "Dune"}, age_hours=0.1)

    fresh = {"title": "Dune Messiah"}
    fetcher = MagicMock(return_value=fresh)

    result = get_or_fetch(
        db_session, "OL", "isbn", "9780441013593", fetcher, bypass_cache=True
    )

    fetcher.assert_called_once()
    assert result == fresh

    row = db_session.get(MetadataCache, ("OL", "isbn", "9780441013593"))
    assert json.loads(row.payload) == fresh


# ---------------------------------------------------------------------------
# Pending-entry visibility under autoflush=False
#
# Regression: a row session.add()-ed by an earlier get_or_fetch in the SAME
# session is pending — not in the identity map, not in the DB — so
# session.get() misses it unless autoflush flushes first. With
# autoflush=False (the app's session_scope default, and downstream callers'),
# a second same-key lookup re-fetched and added a duplicate row → UNIQUE
# violation at commit.
# ---------------------------------------------------------------------------


@pytest.fixture()
def noflush_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        yield session


def test_second_lookup_hits_pending_entry_without_autoflush(noflush_session):
    payload = {"title": "Kind of Blue"}
    fetcher = MagicMock(return_value=payload)

    get_or_fetch(noflush_session, "MB", "mbid", "mbid-1", fetcher)
    result = get_or_fetch(noflush_session, "MB", "mbid", "mbid-1", fetcher)

    fetcher.assert_called_once()
    assert result == payload


def test_same_key_twice_without_autoflush_commits_cleanly(noflush_session):
    fetcher = MagicMock(return_value={"title": "Kind of Blue"})

    get_or_fetch(noflush_session, "MB", "mbid", "mbid-1", fetcher)
    get_or_fetch(noflush_session, "MB", "mbid", "mbid-1", fetcher)

    noflush_session.commit()  # must not raise IntegrityError

    row = noflush_session.get(MetadataCache, ("MB", "mbid", "mbid-1"))
    assert row is not None


def test_bypass_cache_updates_pending_entry_without_autoflush(noflush_session):
    get_or_fetch(
        noflush_session, "MB", "mbid", "mbid-1",
        MagicMock(return_value={"title": "v1"}),
    )
    get_or_fetch(
        noflush_session, "MB", "mbid", "mbid-1",
        MagicMock(return_value={"title": "v2"}), bypass_cache=True,
    )

    noflush_session.commit()  # must not raise IntegrityError

    row = noflush_session.get(MetadataCache, ("MB", "mbid", "mbid-1"))
    assert json.loads(row.payload) == {"title": "v2"}


# ---------------------------------------------------------------------------
# Adapter-namespacing — different adapters = different cache entries
# ---------------------------------------------------------------------------


def test_adapter_namespacing_isolates_entries(db_session):
    _make_row(db_session, adapter="OL", kind="isbn", value="9780441013593",
              payload={"title": "From OL"}, age_hours=0.1)

    fetcher = MagicMock(return_value={"title": "From GB"})
    result = get_or_fetch(db_session, "GB", "isbn", "9780441013593", fetcher)

    fetcher.assert_called_once()
    assert result == {"title": "From GB"}

    ol_row = db_session.get(MetadataCache, ("OL", "isbn", "9780441013593"))
    assert json.loads(ol_row.payload) == {"title": "From OL"}


# ---------------------------------------------------------------------------
# WriteBuffer — deferred writes
# ---------------------------------------------------------------------------


def test_write_buffer_collects_and_does_not_write_immediately(db_session):
    buf = WriteBuffer()
    fetcher = MagicMock(return_value={"title": "Dune"})

    get_or_fetch(
        db_session, "OL", "isbn", "9780441013593", fetcher,
        write_buffer=buf
    )

    # Nothing in the session yet.
    row = db_session.get(MetadataCache, ("OL", "isbn", "9780441013593"))
    assert row is None
    assert len(buf._entries) == 1


def test_write_buffer_flush_persists_entries(db_session):
    # Verify flush() writes the buffered entry by calling the internal helper
    # directly — bypassing session_scope() (which targets the app DB).
    buf = WriteBuffer()
    fetcher = MagicMock(return_value={"title": "Dune"})
    get_or_fetch(db_session, "OL", "isbn", "9780441013593", fetcher, write_buffer=buf)

    # Manually invoke the upsert logic against our test session.
    from compendium.services.metadata_cache import _upsert_to_session

    for entry in buf._entries:
        _upsert_to_session(db_session, entry)

    row = db_session.get(MetadataCache, ("OL", "isbn", "9780441013593"))
    assert row is not None
    assert json.loads(row.payload) == {"title": "Dune"}


def test_write_buffer_deduplicates_same_key():
    buf = WriteBuffer()
    from compendium.domain.models import MetadataCache

    e1 = MetadataCache(
        adapter="OL", kind="isbn", lookup_value="9780441013593",
        payload=json.dumps({"title": "v1"}), is_negative=False, fetched_at=_now()
    )
    e2 = MetadataCache(
        adapter="OL", kind="isbn", lookup_value="9780441013593",
        payload=json.dumps({"title": "v2"}), is_negative=False, fetched_at=_now()
    )
    buf.add(e1)
    buf.add(e2)

    assert len(buf._entries) == 1
    assert json.loads(buf._entries[0].payload) == {"title": "v2"}


# ---------------------------------------------------------------------------
# prune_expired
# ---------------------------------------------------------------------------


def test_prune_expired_removes_stale_rows(db_session):
    _make_row(db_session, adapter="OL", value="111", payload={"x": 1}, age_hours=24 * 31)
    _make_row(db_session, adapter="OL", value="222", payload={"x": 2}, age_hours=1.0)

    with patch("compendium.services.metadata_cache._positive_ttl_days", return_value=30):
        deleted = prune_expired(db_session)

    assert deleted == 1
    assert db_session.get(MetadataCache, ("OL", "isbn", "222")) is not None


def test_prune_expired_removes_stale_negative_rows(db_session):
    _make_row(db_session, adapter="OL", value="old_miss",
              is_negative=True, age_hours=_NEGATIVE_TTL_HOURS + 1)
    _make_row(db_session, adapter="OL", value="fresh_miss",
              is_negative=True, age_hours=0.5)

    with patch("compendium.services.metadata_cache._positive_ttl_days", return_value=30):
        deleted = prune_expired(db_session)

    assert deleted == 1
    assert db_session.get(MetadataCache, ("OL", "isbn", "fresh_miss")) is not None


# ---------------------------------------------------------------------------
# clear_all
# ---------------------------------------------------------------------------


def test_clear_all_deletes_everything(db_session):
    _make_row(db_session, adapter="OL", value="111", payload={"x": 1})
    _make_row(db_session, adapter="OL", value="222", is_negative=True)

    deleted = clear_all(db_session)

    assert deleted == 2
    stats = get_stats(db_session)
    assert stats.total == 0


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------


def test_get_stats_empty(db_session):
    stats = get_stats(db_session)
    assert stats.total == 0
    assert stats.positive == 0
    assert stats.negative == 0
    assert stats.oldest_fetched_at is None


def test_get_stats_counts(db_session):
    _make_row(db_session, adapter="OL", value="111", payload={"x": 1}, age_hours=0.1)
    _make_row(db_session, adapter="OL", value="222", is_negative=True, age_hours=0.1)
    _make_row(db_session, adapter="MB", value="333", payload={"x": 3}, age_hours=24 * 31)

    with patch("compendium.services.metadata_cache._positive_ttl_days", return_value=30):
        stats = get_stats(db_session)

    assert stats.total == 3
    assert stats.positive == 2
    assert stats.negative == 1
    assert stats.expired_positive == 1
    assert stats.expired_negative == 0
    assert stats.adapter_counts["OL"] == 2
    assert stats.adapter_counts["MB"] == 1
