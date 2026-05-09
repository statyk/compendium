"""Integration tests: metadata cache adapter call count and WriteBuffer behavior.

The main constraint: in tests, the WriteBuffer.flush() uses session_scope()
which opens a connection to the app's configured engine. That's a separate DB
from the test fixture's in-memory SQLite engine. So we can't test the full
dry-run → flush → apply round-trip using the standard integration fixture
without special setup.

What we CAN test here:
- That the WriteBuffer collects the right entries during an enriched import
  (via mocking flush() and inspecting the buffer).
- That the cache READ path works: after manually inserting cache rows into
  the test session, a second import pass calls the adapter 0 times.
- That bypass_cache=True forces the adapter call even on cache hits.
- That the negative cache prevents repeated not-found calls within a pass.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from compendium.domain.models import MetadataCache
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService
from compendium.services.import_export import ImportOptions, ImportService
from compendium.services.metadata_cache import WriteBuffer, _upsert_to_session, get_stats


_CSV = """\
isbn,title,media_type,status,is_loanable
9780441013593,Dune,book,available,true
9780441172719,Dune Messiah,book,available,true
"""

_META_DUNE = {
    "title": "Dune",
    "isbn": "9780441013593",
    "description": "A sci-fi epic.",
    "authors": ["Frank Herbert"],
    "external_ids": {},
}
_META_MESSIAH = {
    "title": "Dune Messiah",
    "isbn": "9780441172719",
    "description": "The sequel.",
    "authors": ["Frank Herbert"],
    "external_ids": {},
}


def _make_adapter(call_counter: list[int]):
    def _lookup(kind, value):
        call_counter.append(1)
        if "13593" in value:
            return dict(_META_DUNE)
        return dict(_META_MESSIAH)

    adapter = MagicMock()
    adapter.lookup = _lookup
    return adapter


def _make_catalog(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )


def _make_importer(session) -> ImportService:
    return ImportService(
        session=session,
        catalog=_make_catalog(session),
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        source="test",
    )


def _run(session, *, dry_run: bool, adapter) -> ImportService:
    """Run an import pass and return the service (so callers can inspect _cache_buffer)."""
    svc = _make_importer(session)
    opts = ImportOptions(enrich_from_external=True, dry_run=dry_run)
    with patch("compendium.services.metadata._ADAPTERS", {"book": adapter}):
        svc.import_csv(io.StringIO(_CSV), opts, filename="test.csv")
    return svc


# ---------------------------------------------------------------------------
# WriteBuffer accumulates entries during enriched import
# ---------------------------------------------------------------------------


def test_dry_run_populates_write_buffer(session):
    """Dry-run with enrichment buffers cache entries (flush() called after rollback)."""
    counter: list[int] = []
    adapter = _make_adapter(counter)

    flushed_entries: list = []

    def fake_flush(self):
        flushed_entries.extend(self._entries)

    with patch.object(WriteBuffer, "flush", fake_flush):
        _run(session, dry_run=True, adapter=adapter)

    assert counter == [1, 1], "adapter called once per row"
    assert len(flushed_entries) == 2, "two cache entries buffered"
    assert all(not e.is_negative for e in flushed_entries)


# ---------------------------------------------------------------------------
# Cache READ: after inserting rows, apply pass makes 0 adapter calls
# ---------------------------------------------------------------------------


def test_cached_rows_prevent_adapter_calls_on_apply(session):
    """Pre-seeding cache rows causes the apply pass to read from cache (0 adapter calls)."""
    adapter_name = "MagicMock"
    now = datetime.now(timezone.utc)

    for isbn, meta in [("9780441013593", _META_DUNE), ("9780441172719", _META_MESSIAH)]:
        row = MetadataCache(
            adapter=adapter_name,
            kind="isbn",
            lookup_value=isbn,
            payload=json.dumps(meta),
            is_negative=False,
            fetched_at=now,
        )
        _upsert_to_session(session, row)
    session.flush()

    counter: list[int] = []
    adapter = _make_adapter(counter)

    with patch("compendium.services.metadata_cache.WriteBuffer.flush"):
        _run(session, dry_run=False, adapter=adapter)

    assert counter == [], "apply pass must use cache; 0 adapter calls expected"


# ---------------------------------------------------------------------------
# Negative cache prevents repeated not-found calls within one pass
# ---------------------------------------------------------------------------


def test_negative_cache_rows_prevent_calls(session):
    """Pre-seeding negative rows means the adapter is never called."""
    adapter_name = "MagicMock"
    now = datetime.now(timezone.utc)

    for isbn in ["9780441013593", "9780441172719"]:
        row = MetadataCache(
            adapter=adapter_name,
            kind="isbn",
            lookup_value=isbn,
            payload=None,
            is_negative=True,
            fetched_at=now,
        )
        _upsert_to_session(session, row)
    session.flush()

    counter: list[int] = []
    adapter = _make_adapter(counter)

    with patch("compendium.services.metadata_cache.WriteBuffer.flush"):
        _run(session, dry_run=False, adapter=adapter)

    assert counter == [], "negative cache rows must prevent adapter calls"


# ---------------------------------------------------------------------------
# bypass_cache=True on refresh_metadata hits adapter even with warm cache
# ---------------------------------------------------------------------------


def test_refresh_metadata_bypass_cache_hits_adapter(session):
    # Import without enrichment to get Works into DB.
    counter: list[int] = []
    adapter = _make_adapter(counter)

    svc = _make_importer(session)
    with patch("compendium.services.metadata._ADAPTERS", {"book": adapter}):
        with patch("compendium.services.metadata_cache.WriteBuffer.flush"):
            svc.import_csv(
                io.StringIO(_CSV),
                ImportOptions(enrich_from_external=False, dry_run=False),
            )

    work_repo = SqlWorkRepository(session)
    all_works = work_repo.search("Dune")
    assert len(all_works) >= 1
    work = all_works[0]

    # Insert a fresh cache row for this work's ISBN.
    isbn = work.isbn or "9780441013593"
    row = MetadataCache(
        adapter="MagicMock",
        kind="isbn",
        lookup_value=isbn,
        payload=json.dumps({"title": "Cached title"}),
        is_negative=False,
        fetched_at=datetime.now(timezone.utc),
    )
    _upsert_to_session(session, row)
    session.flush()

    counter.clear()
    catalog_svc = _make_catalog(session)

    with patch("compendium.services.metadata._ADAPTERS", {"book": adapter}):
        catalog_svc.refresh_metadata(work.id, dry_run=True, bypass_cache=True)

    assert len(counter) >= 1, "bypass_cache=True must hit adapter even on cache hit"
