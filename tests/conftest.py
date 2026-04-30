import os

# Set BEFORE any test module imports. Test modules construct `Settings(...)` at
# import time, so env-var defaults that those tests rely on must be in place
# before pytest starts collecting.
#
# - ALLOW_INSECURE_JWT: tests use the literal INSECURE_JWT_DEFAULT; without this
#   the H3 hard-fail would block every create_app() call. Real deployments must
#   set a strong secret instead.
# - SECURE_COOKIES: TestClient runs over plain HTTP (`http://testserver`); the
#   default of `True` (post-M6) makes httpx drop the Secure-flagged auth cookie
#   on subsequent requests, breaking any test that relies on cookie round-trip.
os.environ.setdefault("COMPENDIUM_ALLOW_INSECURE_JWT", "1")
os.environ.setdefault("COMPENDIUM_SECURE_COOKIES", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from compendium.config.seed import seed_defaults
from compendium.domain.models import Base
from tests.helpers import setup_sqlite_fts


@pytest.fixture(scope="session")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def session(engine) -> Session:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture(scope="session")
def pg_engine():
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed")

    try:
        from sqlalchemy import text as _text
        with PostgresContainer("postgres:16-alpine") as pg:
            url = pg.get_connection_url().replace(
                "postgresql+psycopg2://", "postgresql+psycopg://"
            )
            eng = create_engine(url, pool_pre_ping=True)
            Base.metadata.create_all(eng)
            with eng.connect() as conn:
                conn.execute(_text(
                    "CREATE INDEX IF NOT EXISTS ix_work_search_gin ON work"
                    " USING GIN (to_tsvector('english', COALESCE(search_text, '')))"
                ))
                conn.commit()
            yield eng
            Base.metadata.drop_all(eng)
    except Exception as exc:
        pytest.skip(f"Docker/Postgres not available: {exc}")


@pytest.fixture
def pg_session(pg_engine) -> Session:
    factory = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()
