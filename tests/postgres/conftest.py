"""Override the `session` fixture for the postgres/ subtree to use pg_session."""
import pytest


@pytest.fixture
def session(pg_session):
    return pg_session


@pytest.fixture(scope="session")
def pg_container_url():
    """Session-scoped Postgres container; yields its connection URL.

    Separate from ``pg_engine`` because the backup tests need an *empty*
    schema so Alembic can create tables from scratch. The regular fixture
    pre-creates tables via ``Base.metadata.create_all``.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed")
    try:
        with PostgresContainer("postgres:16-alpine") as pg:
            url = pg.get_connection_url().replace(
                "postgresql+psycopg2://", "postgresql+psycopg://"
            )
            yield url
    except Exception as exc:
        pytest.skip(f"Docker/Postgres not available: {exc}")


@pytest.fixture
def pg_clean_url(pg_container_url):
    """Wipe the container's public schema before yielding its URL.

    Function-scoped so each test starts from empty.
    """
    from sqlalchemy import create_engine, text

    eng = create_engine(pg_container_url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    eng.dispose()
    yield pg_container_url
