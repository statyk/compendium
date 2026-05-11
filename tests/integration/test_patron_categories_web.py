"""Web UI tests for patron categories admin + patron form integration."""

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
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _derive_csrf_secret(_SECRET))}"


@pytest.fixture(scope="module")
def pcw_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def pcw_session(pcw_engine):
    factory = sessionmaker(bind=pcw_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def pcw_client(pcw_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: pcw_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


def _login(client, session, username, role_name) -> dict:
    role = SqlRoleRepository(session).get_by_name(role_name)
    user = AppUser(
        username=username, password_hash=hash_password("secret"), role_id=role.id
    )
    SqlUserRepository(session).add(user)
    session.flush()
    raw, signed = _csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": "secret", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    return dict(resp.cookies)


class TestAdminPage:
    def test_lists_seeded_categories(self, pcw_client, pcw_session):
        cookies = _login(pcw_client, pcw_session, "pclib1", "Librarian")
        resp = pcw_client.get("/ui/admin/patron-categories", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        for code in ("adult", "child", "staff", "teacher"):
            assert code in body

    def test_create_via_form(self, pcw_client, pcw_session):
        cookies = _login(pcw_client, pcw_session, "pclib2", "Librarian")
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = pcw_client.post(
            "/ui/admin/patron-categories/new",
            data={"code": "vip", "display_name": "VIP", "csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 303
        assert SqlPatronCategoryRepository(pcw_session).get_by_code("vip") is not None

    def test_readonly_user_forbidden(self, pcw_client, pcw_session):
        cookies = _login(pcw_client, pcw_session, "pcro1", "ReadOnly")
        resp = pcw_client.get("/ui/admin/patron-categories", cookies=cookies)
        assert resp.status_code == 403


class TestPatronFormCategory:
    def test_new_patron_form_includes_category_dropdown(self, pcw_client, pcw_session):
        cookies = _login(pcw_client, pcw_session, "pclib3", "Librarian")
        resp = pcw_client.get("/ui/patrons/new", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'name="category_id"' in body
        assert "Adult" in body  # at least one category option

    def test_post_patron_with_category_and_expiry(self, pcw_client, pcw_session):
        cookies = _login(pcw_client, pcw_session, "pclib4", "Librarian")
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        child = SqlPatronCategoryRepository(pcw_session).get_by_code("child")
        resp = pcw_client.post(
            "/ui/patrons/new",
            data={
                "full_name": "Web Child",
                "category_id": str(child.id),
                "expires_at": "2027-06-15",
                "csrf_token": raw,
            },
            cookies=cookies,
        )
        assert resp.status_code == 303
        # Verify it landed
        from compendium.domain.models import Patron

        patron = (
            pcw_session.query(Patron).filter_by(full_name="Web Child").first()
        )
        assert patron is not None
        assert patron.category_id == child.id
        assert patron.expires_at.isoformat() == "2027-06-15"
