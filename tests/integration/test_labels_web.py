"""Web UI tests for /ui/labels."""

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
from compendium.domain.models import AppUser, Base, Item, MediaType, Patron, Work
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)

_n = {"i": 0}


def _next() -> int:
    _n["i"] += 1
    return _n["i"]


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _SECRET)}"


@pytest.fixture(scope="module")
def lw_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def lw_session(lw_engine):
    factory = sessionmaker(bind=lw_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def lw_client(lw_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: lw_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


def _login(client, session, username, role_name="Librarian") -> dict:
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


def _seed_item(s: Session) -> Item:
    n = _next()
    mt = s.query(MediaType).filter_by(code="book").one()
    w = Work(title=f"Label Test {n}", media_type_id=mt.id)
    s.add(w)
    s.flush()
    branch = SqlBranchRepository(s).get_default()
    it = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode=f"WLB{n:06d}",
        accession_number=f"WLA{n:06d}",
        call_number="PS3551",
    )
    s.add(it)
    s.flush()
    return it


def _seed_patron(s: Session) -> Patron:
    n = _next()
    p = Patron(library_card_number=f"WLC{n:05d}", full_name=f"WebLabel Patron {n}")
    s.add(p)
    s.flush()
    return p


class TestAuth:
    def test_readonly_forbidden(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lwro1", "ReadOnly")
        resp = lw_client.get("/ui/labels", cookies=cookies)
        assert resp.status_code == 403

    def test_unauthenticated_redirects_to_login(self, lw_client):
        resp = lw_client.get("/ui/labels")
        assert resp.status_code == 303
        assert "/ui/login" in resp.headers["location"]


class TestIndex:
    def test_index_lists_templates(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw1")
        resp = lw_client.get("/ui/labels", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Item labels" in body
        assert "Patron cards" in body
        assert "avery-5160" in body


class TestItemForm:
    def test_form_renders(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw2")
        resp = lw_client.get("/ui/labels/items", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'name="template"' in body
        assert 'name="format"' in body
        assert 'name="barcodes"' in body

    def test_post_returns_pdf(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw3")
        _seed_item(lw_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = lw_client.post(
            "/ui/labels/items",
            data={
                "template": "avery-5160",
                "format": "pocket",
                "start_label": "0",
                "csrf_token": raw,
            },
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")

    def test_post_empty_filter_re_renders_form_with_error(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw4")
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = lw_client.post(
            "/ui/labels/items",
            data={
                "template": "avery-5160",
                "barcodes": "DOESNOTEXIST",
                "start_label": "0",
                "csrf_token": raw,
            },
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert b"No items matched" in resp.content


class TestPatronForm:
    def test_form_renders(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw5")
        resp = lw_client.get("/ui/labels/patrons", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'name="template"' in body
        assert 'name="format"' in body

    def test_post_returns_pdf(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw6")
        _seed_patron(lw_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = lw_client.post(
            "/ui/labels/patrons",
            data={
                "template": "avery-5871",
                "format": "full",
                "start_label": "0",
                "csrf_token": raw,
            },
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")
