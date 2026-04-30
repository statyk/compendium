import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from compendium.config.seed import seed_defaults
from compendium.domain.models import Base
from tests.helpers import setup_sqlite_fts


@pytest.fixture(autouse=True, scope="session")
def _allow_insecure_jwt_in_tests():
    # Tests construct Settings with the literal INSECURE_JWT_DEFAULT and call
    # create_app(); without this the H3 hard-fail would block every TestClient
    # startup. Real deployments must set a strong secret instead.
    prev = os.environ.get("COMPENDIUM_ALLOW_INSECURE_JWT")
    os.environ["COMPENDIUM_ALLOW_INSECURE_JWT"] = "1"
    yield
    if prev is None:
        os.environ.pop("COMPENDIUM_ALLOW_INSECURE_JWT", None)
    else:
        os.environ["COMPENDIUM_ALLOW_INSECURE_JWT"] = prev


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
