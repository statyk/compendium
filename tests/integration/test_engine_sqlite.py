"""SQLite engine pragma tests.

The default rollback-journal mode locks out readers during writes and returns
SQLITE_BUSY immediately on contention. We enable WAL + a 5s busy_timeout so
the daemon and a separately-invoked maintenance command (each its own Python
process, but sharing the DB file) coexist instead of stepping on each other.

WAL is silently a no-op on `:memory:` databases — these tests use a tmp file.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from compendium.config.settings import Settings
from compendium.db.engine import make_engine


@pytest.fixture
def sqlite_file(tmp_path: Path) -> Path:
    return tmp_path / "compendium-test.db"


def test_wal_journal_mode(sqlite_file: Path):
    eng = make_engine(Settings(database_url=f"sqlite:///{sqlite_file}"))
    with eng.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode == "wal", f"expected WAL, got {mode!r}"


def test_busy_timeout_set(sqlite_file: Path):
    eng = make_engine(Settings(database_url=f"sqlite:///{sqlite_file}"))
    with eng.connect() as conn:
        timeout_ms = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert timeout_ms == 5000, f"expected 5000ms, got {timeout_ms}"


def test_synchronous_normal(sqlite_file: Path):
    eng = make_engine(Settings(database_url=f"sqlite:///{sqlite_file}"))
    with eng.connect() as conn:
        # PRAGMA synchronous returns 0/1/2/3 for OFF/NORMAL/FULL/EXTRA.
        sync = conn.execute(text("PRAGMA synchronous")).scalar()
    assert sync == 1, f"expected NORMAL (1), got {sync}"


def test_postgres_url_constructs_without_sqlite_pragmas():
    """make_engine must accept a postgres URL — the SQLite pragma listener
    is registered inside the sqlite-only branch, so this just verifies the
    Postgres path doesn't fall through that branch and break construction.

    No actual DB connection is attempted (no Postgres needed in CI).
    """
    eng = make_engine(Settings(database_url="postgresql+psycopg://u:p@localhost:5432/x"))
    assert eng.dialect.name == "postgresql"


def test_concurrent_writes_dont_immediately_busy(sqlite_file: Path):
    """With busy_timeout, a second connection's write should wait briefly
    rather than fail with 'database is locked' the instant another connection
    is mid-transaction. We hold a write transaction open on conn1 just long
    enough that conn2 has to wait, then release; conn2 must succeed."""
    import threading
    import time

    eng = make_engine(Settings(database_url=f"sqlite:///{sqlite_file}"))
    # Set up a tiny schema to write to.
    with eng.begin() as setup:
        setup.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)"))

    held = threading.Event()
    released = threading.Event()
    errors: list[Exception] = []

    def hold_writer():
        try:
            with eng.connect() as conn:
                # BEGIN IMMEDIATE acquires a reserved lock right away.
                conn.execute(text("BEGIN IMMEDIATE"))
                conn.execute(text("INSERT INTO t (v) VALUES (1)"))
                held.set()
                # Hold the lock for 200ms — well under busy_timeout's 5s.
                time.sleep(0.2)
                conn.execute(text("COMMIT"))
                released.set()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t = threading.Thread(target=hold_writer)
    t.start()
    assert held.wait(2.0), "hold thread didn't acquire its lock"

    # Now attempt a write on a *separate* connection while the first holds
    # the lock. With busy_timeout=5000 this waits up to 5s; without it,
    # SQLite raises OperationalError immediately.
    with eng.connect() as conn2:
        conn2.execute(text("INSERT INTO t (v) VALUES (2)"))
        conn2.commit()

    t.join(timeout=2.0)
    assert not errors, f"hold thread errored: {errors}"
    assert released.is_set()

    with eng.connect() as conn:
        rows = conn.execute(text("SELECT v FROM t ORDER BY id")).all()
    assert [r[0] for r in rows] == [1, 2]
