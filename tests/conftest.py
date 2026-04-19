import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from compendium.config.seed import seed_defaults
from compendium.domain.models import Base


@pytest.fixture(scope="session")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
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
        with PostgresContainer("postgres:16-alpine") as pg:
            url = pg.get_connection_url().replace(
                "postgresql+psycopg2://", "postgresql+psycopg://"
            )
            eng = create_engine(url, pool_pre_ping=True)
            Base.metadata.create_all(eng)
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
