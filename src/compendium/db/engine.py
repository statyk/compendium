from functools import lru_cache

from sqlalchemy import Engine, create_engine, event

from compendium.config.settings import Settings


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


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings())
