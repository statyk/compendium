"""Shared test utilities."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService, hash_password
from compendium.web.csrf import _sign, generate_token

TEST_SECRET = "insecure-default-change-in-production"


def std_settings(**overrides) -> Settings:
    """Base test Settings: in-memory SQLite + insecure JWT secret."""
    return Settings(database_url="sqlite:///:memory:", jwt_secret_key=TEST_SECRET, **overrides)


def setup_sqlite_fts(engine: Engine) -> None:
    """Create FTS5 virtual table and triggers on an in-memory SQLite engine.

    Required because tests use create_all() instead of Alembic migrations.
    """
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS work_fts"
            " USING fts5(search_text, content='work', content_rowid='id')"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS work_fts_ai AFTER INSERT ON work BEGIN"
            "  INSERT INTO work_fts(rowid, search_text)"
            "  VALUES (new.id, COALESCE(new.search_text, ''));"
            " END"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS work_fts_ad AFTER DELETE ON work BEGIN"
            "  INSERT INTO work_fts(work_fts, rowid, search_text)"
            "  VALUES ('delete', old.id, COALESCE(old.search_text, ''));"
            " END"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS work_fts_au AFTER UPDATE OF search_text ON work BEGIN"
            "  INSERT INTO work_fts(work_fts, rowid, search_text)"
            "  VALUES ('delete', old.id, COALESCE(old.search_text, ''));"
            "  INSERT INTO work_fts(rowid, search_text)"
            "  VALUES (new.id, COALESCE(new.search_text, ''));"
            " END"
        ))
        conn.commit()


def make_engine() -> Engine:
    """StaticPool SQLite engine with schema + FTS triggers. Use module-scoped in fixtures."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)
    return engine


def session_for(engine: Engine) -> Generator[Session, None, None]:
    """Seeded, auto-rolled-back session. Use in fixtures as `yield from session_for(engine)`."""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@contextmanager
def make_client(
    engine: Engine, *, settings: Settings | None = None
) -> Generator[TestClient, None, None]:
    """TestClient with per-request sessions (commit/rollback on each request) + settings patch.

    Usage in a pytest fixture::

        @pytest.fixture
        def client(engine, db_session):
            with make_client(engine) as c:
                yield c
    """
    app = create_app()
    cfg = settings or std_settings()
    fac = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _session_override() -> Generator[Session, None, None]:
        s = fac()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_session] = _session_override
    with patch("compendium.db.engine.get_settings", return_value=cfg):
        # raise_server_exceptions=True is the starlette default; stated explicitly
        # so migrated files that previously omitted it get the same behaviour.
        yield TestClient(app, raise_server_exceptions=True, follow_redirects=False)


def make_user(session: Session, username: str, role_name: str) -> tuple[AppUser, str]:
    """Create a user and return (user, jwt_token). Flushes but does not commit."""
    role_repo = SqlRoleRepository(session)
    user_repo = SqlUserRepository(session)
    role = role_repo.get_by_name(role_name)
    assert role is not None, f"Role {role_name!r} not seeded — was seed_defaults called?"
    user = AppUser(
        username=username,
        email=None,
        password_hash=hash_password("password"),
        role_id=role.id,
    )
    user_repo.add(user)
    session.flush()
    user.role = role
    token = AuthService(
        user_repo=user_repo,
        role_repo=role_repo,
        settings=std_settings(),
    ).issue_token(user)
    return user, token


def csrf_pair() -> tuple[str, str]:
    """Return (raw_token, signed_cookie_value) for CSRF testing."""
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, TEST_SECRET)}"
