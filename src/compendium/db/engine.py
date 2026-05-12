from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session

from compendium.config.settings import Settings

_bound_engine: Engine | None = None
_bound_session_scope: Callable[[], AbstractContextManager[Session]] | None = None


def bind(
    engine: Engine,
    *,
    session_scope: Callable[[], AbstractContextManager[Session]] | None = None,
) -> None:
    """Install a host-supplied engine (and optional session_scope).

    Library consumers (LitCat, future embedders) call this once at startup
    after constructing their own engine.  If ``session_scope`` is omitted,
    Compendium's default ``session_scope`` builds a scope from the bound
    engine with its standard semantics (autoflush=False, expire_on_commit=False,
    commit on context exit, rollback on exception).

    The binding is process-wide and visible from any thread — suitable for
    desktop embedders that install the binding once at startup and then spin up
    worker threads.  ``bind()`` is not safe to call from multiple threads
    concurrently; the intended usage is a single call during application init.

    **Contract for host-supplied ``session_scope``:** the context manager MUST
    commit on normal exit and rollback on exception.  Compendium's internal
    writers (metadata_cache, GB quota sentinel, site_settings cache refresh)
    rely on this — they enter the scope, do writes, and exit normally without
    calling ``session.commit()`` themselves.  A scope that does not commit will
    silently lose those writes.

    Call ``bind()`` again to replace an existing binding.  Call ``unbind()``
    to revert to server-mode defaults.
    """
    global _bound_engine, _bound_session_scope
    _bound_engine = engine
    _bound_session_scope = session_scope


def unbind() -> None:
    """Revert to server-mode engine/session defaults."""
    global _bound_engine, _bound_session_scope
    _bound_engine = None
    _bound_session_scope = None


def bound_engine() -> Engine | None:
    return _bound_engine


def bound_session_scope() -> Callable[[], AbstractContextManager[Session]] | None:
    return _bound_session_scope


def make_engine(settings: Settings) -> Engine:
    kwargs: dict = {}
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    elif settings.database_url.startswith("postgresql"):
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 10
        kwargs["pool_pre_ping"] = True
    engine = create_engine(settings.database_url, **kwargs)

    if settings.database_url.startswith("sqlite"):
        # SQLite default rollback-journal mode locks readers out during writes
        # and returns SQLITE_BUSY immediately on contention. WAL lets readers
        # and one writer coexist; busy_timeout makes brief writer-vs-writer
        # contention wait rather than fail. Both matter when the daemon and a
        # cron'd maintenance command (separate processes) hit the same DB.
        # synchronous=NORMAL is the documented safe pairing with WAL.
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover — exercised by every connection
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.execute("PRAGMA synchronous=NORMAL")
            finally:
                cur.close()

    return engine


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_engine() -> Engine:
    if _bound_engine is not None:
        return _bound_engine
    return _server_engine()


@lru_cache
def _server_engine() -> Engine:
    return make_engine(get_settings())
