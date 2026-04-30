"""Bounded upload guard for bulk-import endpoints (M4).

Three endpoints accept multipart uploads: `/import/csv`, `/import/marc`
(API), and `/ui/admin/import` (web). All three must reject oversize
bodies with 413 — pre-read by Content-Length where honest, mid-read by
size-guard otherwise.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService, hash_password
from compendium.web.csrf import _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
# Tiny cap so we can synthesize an "oversize" upload trivially.
_TINY_CAP = 1024
_SETTINGS = Settings(
    database_url="sqlite:///:memory:",
    jwt_secret_key=_SECRET,
    max_upload_bytes=_TINY_CAP,
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def db_session(engine) -> Session:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def client(engine, db_session):
    app = create_app()

    def _override():
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

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_settings] = lambda: _SETTINGS
    with patch("compendium.db.engine.get_settings", return_value=_SETTINGS):
        yield TestClient(app, follow_redirects=False)


def _make_user(s: Session, role_name: str, username: str) -> tuple[AppUser, str]:
    role = SqlRoleRepository(s).get_by_name(role_name)
    user = AppUser(
        username=username,
        password_hash=hash_password("password"),
        role_id=role.id,
    )
    SqlUserRepository(s).add(user)
    s.flush()
    user.role = role
    token = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(user)
    return user, token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _SECRET)}"


def test_api_csv_rejects_upload_above_cap(client, db_session):
    _, token = _make_user(db_session, "Librarian", "u_csv_big")
    db_session.commit()
    payload = b"x" * (_TINY_CAP * 2)
    resp = client.post(
        "/import/csv",
        files={"file": ("big.csv", payload, "text/csv")},
        headers=_bearer(token),
    )
    assert resp.status_code == 413, resp.text
    assert "exceeds" in resp.json()["detail"].lower()


def test_api_csv_accepts_upload_below_cap(client, db_session):
    _, token = _make_user(db_session, "Librarian", "u_csv_ok")
    db_session.commit()
    # Real CSV under the cap.
    payload = b"media_type,title,authors,isbn\nbook,Dune,Frank Herbert,9780441013593\n"
    assert len(payload) <= _TINY_CAP
    resp = client.post(
        "/import/csv",
        files={"file": ("books.csv", payload, "text/csv")},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text


def test_api_marc_rejects_upload_above_cap(client, db_session):
    _, token = _make_user(db_session, "Librarian", "u_marc_big")
    db_session.commit()
    payload = b"\x00" * (_TINY_CAP * 2)
    resp = client.post(
        "/import/marc",
        files={"file": ("big.mrc", payload, "application/marc")},
        headers=_bearer(token),
    )
    assert resp.status_code == 413


def test_web_bulk_import_rejects_upload_above_cap(client, db_session):
    user, _ = _make_user(db_session, "Librarian", "u_web_big")
    # Web auth: log this user in via cookie.
    raw, signed = _csrf_pair()
    cookies = {"csrf_token": signed}
    # Issue a real session cookie via the auth login form.
    db_session.commit()
    login = client.post(
        "/ui/login",
        data={"username": "u_web_big", "password": "password", "csrf_token": raw},
        cookies=cookies,
    )
    assert login.status_code in (302, 303), login.text

    payload = b"x" * (_TINY_CAP * 2)
    resp = client.post(
        "/ui/admin/import",
        data={"format": "csv", "mode": "append", "csrf_token": raw},
        files={"file": ("big.csv", payload, "text/csv")},
        cookies=cookies,
    )
    assert resp.status_code == 413, resp.text
    # Web path renders the friendly error page.
    assert b"exceeds" in resp.content.lower() or b"too large" in resp.content.lower()
