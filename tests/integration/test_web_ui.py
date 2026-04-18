"""Integration smoke tests for the web UI routes."""

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
from compendium.domain.models import AppUser, Base, Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.auth import hash_password
from compendium.services.catalog import CatalogService
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _sign, generate_token

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}
_ISBN = "9780441013593"
_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)


def _make_csrf_pair() -> tuple[str, str]:
    """Returns (raw_token, signed_cookie_value) for test requests."""
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _SECRET)}"
    return raw, signed


@pytest.fixture(scope="module")
def web_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def web_session(web_engine) -> Session:
    factory = sessionmaker(bind=web_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def web_client(web_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: web_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


@pytest.fixture
def librarian(web_session):
    role = SqlRoleRepository(web_session).get_by_name("Librarian")
    user = AppUser(username="lib01", password_hash=hash_password("secret"), role_id=role.id)
    SqlUserRepository(web_session).add(user)
    web_session.flush()
    return user


@pytest.fixture
def patron_user(web_session):
    role = SqlRoleRepository(web_session).get_by_name("Patron")
    user = AppUser(username="patron01", password_hash=hash_password("secret"), role_id=role.id)
    SqlUserRepository(web_session).add(user)
    web_session.flush()
    patron = Patron(
        library_card_number="WEB0001",
        full_name="Web Patron",
        user_id=user.id,
    )
    SqlPatronRepository(web_session).add(patron)
    web_session.flush()
    return user, patron


@pytest.fixture
def work(web_session):
    with patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        work, item = CatalogService(
            work_repo=SqlWorkRepository(web_session),
            item_repo=SqlItemRepository(web_session),
            creator_repo=SqlCreatorRepository(web_session),
            branch_repo=SqlBranchRepository(web_session),
        ).add_from_isbn(_ISBN)
    web_session.flush()
    return work, item


def _login(client, username: str, password: str = "secret") -> dict:
    """Log in via /ui/login and return the auth cookies."""
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": password, "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    return dict(resp.cookies)


# ── Login / logout ────────────────────────────────────────────────────────────


def test_login_page_renders(web_client):
    resp = web_client.get("/ui/login")
    assert resp.status_code == 200
    assert b"Login" in resp.content


def test_login_bad_credentials(web_client, librarian):
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/login",
        data={"username": "lib01", "password": "wrong", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 401
    assert b"Invalid" in resp.content


def test_login_success_redirects(web_client, librarian):
    cookies = _login(web_client, "lib01")
    assert "compendium_auth" in cookies


def test_logout_clears_cookie(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/logout",
        data={"csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303


# ── Catalog ───────────────────────────────────────────────────────────────────


def test_catalog_search_renders_unauthenticated(web_client):
    resp = web_client.get("/ui/catalog")
    assert resp.status_code == 200
    assert b"Catalog" in resp.content


def test_catalog_search_results_partial(web_client, work):
    resp = web_client.get("/ui/catalog/search-results?q=Dune")
    assert resp.status_code == 200
    assert b"Dune" in resp.content


def test_catalog_detail_renders(web_client, work):
    w, _ = work
    resp = web_client.get(f"/ui/catalog/{w.id}")
    assert resp.status_code == 200
    assert b"Dune" in resp.content


def test_catalog_detail_404(web_client):
    resp = web_client.get("/ui/catalog/99999")
    assert resp.status_code == 404


# ── Auth-protected pages redirect when unauthenticated ────────────────────────


def test_circ_desk_requires_auth(web_client):
    resp = web_client.get("/ui/circ")
    assert resp.status_code == 303
    assert "/ui/login" in resp.headers["location"]


def test_patrons_requires_auth(web_client):
    resp = web_client.get("/ui/patrons")
    assert resp.status_code == 303
    assert "/ui/login" in resp.headers["location"]


def test_my_loans_requires_auth(web_client):
    resp = web_client.get("/ui/me/loans")
    assert resp.status_code == 303
    assert "/ui/login" in resp.headers["location"]


# ── Librarian pages ───────────────────────────────────────────────────────────


def test_circ_desk_renders_for_librarian(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/circ", cookies=cookies)
    assert resp.status_code == 200
    assert b"Circulation Desk" in resp.content


def test_patrons_list_renders_for_librarian(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/patrons", cookies=cookies)
    assert resp.status_code == 200
    assert b"Patrons" in resp.content


# ── Patron self-service ───────────────────────────────────────────────────────


def test_my_loans_renders_empty(web_client, patron_user):
    _, _ = patron_user
    cookies = _login(web_client, "patron01")
    resp = web_client.get("/ui/me/loans", cookies=cookies)
    assert resp.status_code == 200
    assert b"no active loans" in resp.content.lower()


def test_my_holds_renders_empty(web_client, patron_user):
    cookies = _login(web_client, "patron01")
    resp = web_client.get("/ui/me/holds", cookies=cookies)
    assert resp.status_code == 200
    assert b"no active holds" in resp.content.lower()


def test_place_hold_via_catalog(web_client, patron_user, work):
    w, _ = work
    cookies = _login(web_client, "patron01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/catalog/{w.id}/hold",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert b"Hold placed" in resp.content


def test_csrf_mismatch_rejected(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    bad_raw = generate_token()
    resp = web_client.post(
        "/ui/circ/checkin",
        data={"barcode": "ANYBARCODE", "csrf_token": bad_raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 403


# ── Audit log ─────────────────────────────────────────────────────────────────


def test_audit_log_requires_auth(web_client):
    resp = web_client.get("/ui/audit")
    assert resp.status_code == 303
    assert "/ui/login" in resp.headers["location"]


def test_audit_log_renders_for_librarian(web_client, librarian, work):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/audit", cookies=cookies)
    assert resp.status_code == 200
    assert b"Audit Log" in resp.content


def test_audit_log_filter_by_entity_type(web_client, librarian, work):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/audit?entity_type=item", cookies=cookies)
    assert resp.status_code == 200
    assert b"item" in resp.content


# ── Patron create ─────────────────────────────────────────────────────────────


def test_patron_new_form_renders(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/patrons/new", cookies=cookies)
    assert resp.status_code == 200
    assert b"Add Patron" in resp.content


def test_patron_create_redirects_to_detail(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/patrons/new",
        data={"full_name": "Jane Test", "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert "/ui/patrons/" in resp.headers["location"]


def test_patron_new_requires_auth(web_client):
    resp = web_client.get("/ui/patrons/new")
    assert resp.status_code == 303


# ── Item detail ───────────────────────────────────────────────────────────────


def test_item_detail_renders(web_client, librarian, work):
    _, item = work
    cookies = _login(web_client, "lib01")
    resp = web_client.get(f"/ui/items/{item.barcode}", cookies=cookies)
    assert resp.status_code == 200
    assert item.barcode.encode() in resp.content


def test_item_detail_404(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/items/NOSUCHBARCODE", cookies=cookies)
    assert resp.status_code == 404


def test_item_withdraw_via_web(web_client, librarian, work):
    _, item = work
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/items/{item.barcode}/withdraw",
        data={"csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert b"withdrawn" in resp.content.lower()


# ── Item add (new) ────────────────────────────────────────────────────────────


def test_item_new_form_renders(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/items/new", cookies=cookies)
    assert resp.status_code == 200
    assert b"Add Item" in resp.content


def test_item_new_requires_auth(web_client):
    resp = web_client.get("/ui/items/new")
    assert resp.status_code == 303


def test_item_lookup_returns_preview(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    with patch("compendium.web.routes.items.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        resp = web_client.post(
            "/ui/items/lookup",
            data={"isbn": _ISBN, "csrf_token": raw},
            cookies={**auth_cookies, CSRF_COOKIE: signed},
        )
    assert resp.status_code == 200
    assert b"Dune" in resp.content


def test_item_lookup_existing_work(web_client, librarian, work):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/items/lookup",
        data={"isbn": _ISBN, "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert b"Already in catalog" in resp.content


def test_item_create_via_web(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    with patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        resp = web_client.post(
            "/ui/items/new",
            data={"isbn": "9780441013594", "csrf_token": raw, "location": "Shelf B"},
            cookies={**auth_cookies, CSRF_COOKIE: signed},
        )
    assert resp.status_code == 303
    assert "/ui/items/" in resp.headers["location"]
