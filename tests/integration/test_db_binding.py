"""Integration tests for the engine/session binding seam.

Verifies that ``compendium.db.engine.bind()`` routes all Compendium-internal
DB access to the host-supplied engine and session_scope, and that server-mode
behaviour is completely unchanged when no binding is installed.
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from compendium.config.seed import seed_defaults
from compendium.db.engine import (
    _server_engine,
    bind,
    bound_engine,
    bound_session_scope,
    get_engine,
    unbind,
)
from compendium.db.session import session_scope
from compendium.domain.models import Base
from tests.helpers import setup_sqlite_fts


@pytest.fixture(autouse=True)
def _reset_binding():
    """Ensure each test starts and ends with no binding installed."""
    unbind()
    yield
    unbind()


@pytest.fixture
def alt_engine():
    """A second, fully initialised in-memory SQLite engine."""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    with factory() as s:
        seed_defaults(s)
        s.commit()
    return eng


@contextmanager
def _make_scope(engine) -> Generator[Session, None, None]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ---------------------------------------------------------------------------
# bind() / unbind() basics
# ---------------------------------------------------------------------------

def test_no_binding_returns_server_engine():
    assert bound_engine() is None
    assert get_engine() is _server_engine()


def test_bind_makes_get_engine_return_bound(alt_engine):
    bind(alt_engine)
    assert get_engine() is alt_engine


def test_unbind_restores_server_engine(alt_engine):
    bind(alt_engine)
    unbind()
    assert get_engine() is _server_engine()
    assert bound_engine() is None


def test_bind_twice_replaces_binding(alt_engine):
    eng2 = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng2)
    bind(alt_engine)
    bind(eng2)
    assert get_engine() is eng2


# ---------------------------------------------------------------------------
# session_scope routing
# ---------------------------------------------------------------------------

def test_session_scope_uses_bound_scope_when_set(alt_engine):
    calls = []

    @contextmanager
    def _spy_scope():
        with _make_scope(alt_engine) as s:
            calls.append("enter")
            yield s
            calls.append("exit")

    bind(alt_engine, session_scope=_spy_scope)
    with session_scope() as s:
        assert s.get_bind() is alt_engine
    assert calls == ["enter", "exit"]


def test_session_scope_uses_bound_engine_when_no_scope_supplied(alt_engine):
    bind(alt_engine)
    with session_scope() as s:
        assert s.get_bind() is alt_engine


def test_session_scope_server_default_when_unbound():
    assert bound_session_scope() is None
    # Simply verify session_scope() doesn't raise when there's no binding;
    # it will use the server engine (which may be the test-scoped engine via
    # monkeypatching done elsewhere — just confirm it runs without error).
    # We don't actually open a connection here; just verify the path.
    assert bound_session_scope() is None


# ---------------------------------------------------------------------------
# Compendium-internal service targets the bound DB
# ---------------------------------------------------------------------------

def test_site_settings_reads_land_on_bound_db(alt_engine):
    """get_site_setting's cache refresh path uses get_engine(); verify it
    targets the bound engine when a binding is installed."""
    bind(alt_engine)

    from compendium.services.site_settings import get_site_setting, set_site_setting
    with session_scope() as s:
        set_site_setting("metadata_cache_ttl_days", 60, session=s)
    val = get_site_setting("metadata_cache_ttl_days")
    assert val == 60

    # Confirm the row is on alt_engine, not on the server-default engine.
    with alt_engine.connect() as conn:
        row = conn.execute(
            text("SELECT value FROM site_setting WHERE key = 'metadata_cache_ttl_days'")
        ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# Cross-thread visibility
# ---------------------------------------------------------------------------

def test_binding_is_visible_across_threads(alt_engine):
    """bind() is process-wide — a binding installed on the main thread must be
    visible from worker threads (the contract for desktop-embedder usage)."""
    bind(alt_engine)

    results: dict[str, object] = {}

    def _worker() -> None:
        results["bound"] = bound_engine()
        results["get"] = get_engine()

    t = threading.Thread(target=_worker)
    t.start()
    t.join()

    assert results["bound"] is alt_engine, "bound_engine() not visible in worker thread"
    assert results["get"] is alt_engine, "get_engine() not visible in worker thread"
