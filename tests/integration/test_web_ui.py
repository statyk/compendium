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
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
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
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        work, item = CatalogService(
            work_repo=SqlWorkRepository(web_session),
            item_repo=SqlItemRepository(web_session),
            creator_repo=SqlCreatorRepository(web_session),
            branch_repo=SqlBranchRepository(web_session),
            media_type_repo=SqlMediaTypeRepository(web_session),
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
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        resp = web_client.post(
            "/ui/items/lookup",
            data={"media_type": "book", "identifier": _ISBN, "csrf_token": raw},
            cookies={**auth_cookies, CSRF_COOKIE: signed},
        )
    assert resp.status_code == 200
    assert b"Dune" in resp.content


def test_item_lookup_existing_work(web_client, librarian, work):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/items/lookup",
        data={"media_type": "book", "identifier": _ISBN, "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert b"Already in catalog" in resp.content


def test_item_create_via_web(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        resp = web_client.post(
            "/ui/items/new",
            data={
                "media_type": "book",
                "identifier_kind": "isbn",
                "identifier_value": "9780441013594",
                "csrf_token": raw,
                "location": "Shelf B",
            },
            cookies={**auth_cookies, CSRF_COOKIE: signed},
        )
    assert resp.status_code == 303
    assert "/ui/items/" in resp.headers["location"]


# ── Film (TMDb) item flow ─────────────────────────────────────────────────────

_TMDB_CANDIDATES = [
    {
        "tmdb_id": 497,
        "title": "The Green Mile",
        "year": "1999",
        "overview": "A supernatural tale.",
        "poster_url": None,
    }
]

_TMDB_MOVIE_DATA = {
    "id": 497,
    "title": "The Green Mile",
    "release_date": "1999-12-10",
    "overview": "A supernatural tale.",
    "runtime": 189,
    "tagline": None,
    "original_language": "en",
    "poster_path": None,
    "imdb_id": "tt0120689",
    "genres": [{"id": 18, "name": "Drama"}],
    "credits": {
        "crew": [{"name": "Frank Darabont", "job": "Director", "department": "Directing"}],
        "cast": [{"name": "Tom Hanks", "order": 0}],
    },
}


def test_item_lookup_film_title_shows_candidates(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    with patch("compendium.services.metadata._tmdb_search_candidates", return_value=_TMDB_CANDIDATES):
        with patch.dict("os.environ", {"COMPENDIUM_TMDB_API_KEY": "testkey"}):
            resp = web_client.post(
                "/ui/items/lookup",
                data={"media_type": "dvd", "identifier": "The Green Mile", "csrf_token": raw},
                cookies={**auth_cookies, CSRF_COOKIE: signed},
            )
    assert resp.status_code == 200
    assert b"The Green Mile" in resp.content
    assert b"1999" in resp.content


def test_item_lookup_film_tmdb_id_shows_preview(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    with patch("compendium.services.metadata._tmdb_fetch_movie", return_value=_TMDB_MOVIE_DATA):
        with patch.dict("os.environ", {"COMPENDIUM_TMDB_API_KEY": "testkey"}):
            resp = web_client.post(
                "/ui/items/lookup",
                data={"media_type": "dvd", "identifier": "497", "csrf_token": raw},
                cookies={**auth_cookies, CSRF_COOKIE: signed},
            )
    assert resp.status_code == 200
    assert b"The Green Mile" in resp.content
    assert b"189 min" in resp.content


def test_item_create_dvd_via_web(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    with patch("compendium.services.metadata._tmdb_fetch_movie", return_value=_TMDB_MOVIE_DATA):
        with patch.dict("os.environ", {"COMPENDIUM_TMDB_API_KEY": "testkey"}):
            resp = web_client.post(
                "/ui/items/new",
                data={
                    "media_type": "dvd",
                    "identifier_kind": "tmdb_id",
                    "identifier_value": "497",
                    "csrf_token": raw,
                },
                cookies={**auth_cookies, CSRF_COOKIE: signed},
            )
    assert resp.status_code == 303
    assert "/ui/items/" in resp.headers["location"]


# ── User management ───────────────────────────────────────────────────────────


def test_user_list_requires_auth(web_client):
    resp = web_client.get("/ui/users")
    assert resp.status_code == 303
    assert "/ui/login" in resp.headers["location"]


def test_user_list_renders_for_librarian(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/users", cookies=cookies)
    assert resp.status_code == 200
    assert b"Users" in resp.content
    assert b"lib01" in resp.content


def test_user_new_form_renders(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/users/new", cookies=cookies)
    assert resp.status_code == 200
    assert b"Create User" in resp.content
    assert b"Patron" in resp.content  # role dropdown populated


def test_user_create_redirects_to_detail(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/users/new",
        data={"username": "newuser01", "password": "secret99", "role_name": "Patron", "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert "/ui/users/newuser01" in resp.headers["location"]


def test_user_detail_renders(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/users/lib01", cookies=cookies)
    assert resp.status_code == 200
    assert b"lib01" in resp.content
    assert b"Librarian" in resp.content


def test_user_detail_404(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/users/nosuchuser", cookies=cookies)
    assert resp.status_code == 404


def test_user_change_role_redirects(web_client, librarian, web_session):
    # Create a separate user to change role for (can't easily change own role in test)
    role = SqlRoleRepository(web_session).get_by_name("ReadOnly")
    from compendium.services.auth import hash_password as hp
    u = AppUser(username="roletest01", password_hash=hp("pw"), role_id=role.id)
    SqlUserRepository(web_session).add(u)
    web_session.flush()

    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/users/roletest01/change-role",
        data={"role_name": "Patron", "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert "roletest01" in resp.headers["location"]


# ── Patron↔user linking ───────────────────────────────────────────────────────


def test_patron_new_form_shows_user_dropdown(web_client, librarian, web_session):
    # Ensure there's at least one unlinked active user
    role = SqlRoleRepository(web_session).get_by_name("Patron")
    from compendium.services.auth import hash_password as hp
    u = AppUser(username="unlinked01", password_hash=hp("pw"), role_id=role.id)
    SqlUserRepository(web_session).add(u)
    web_session.flush()

    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/patrons/new", cookies=cookies)
    assert resp.status_code == 200
    assert b"unlinked01" in resp.content


def test_patron_create_with_linked_user(web_client, librarian, web_session):
    role = SqlRoleRepository(web_session).get_by_name("Patron")
    from compendium.services.auth import hash_password as hp
    u = AppUser(username="linkme01", password_hash=hp("pw"), role_id=role.id)
    SqlUserRepository(web_session).add(u)
    web_session.flush()

    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/patrons/new",
        data={"full_name": "Linked Person", "user_id": str(u.id), "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    card = resp.headers["location"].split("/")[-1]
    patron = SqlPatronRepository(web_session).get_by_card_number(card)
    assert patron is not None
    assert patron.user_id == u.id


def test_patron_link_unlink_user(web_client, librarian, web_session):
    role = SqlRoleRepository(web_session).get_by_name("Patron")
    from compendium.services.auth import hash_password as hp
    u = AppUser(username="linktest02", password_hash=hp("pw"), role_id=role.id)
    SqlUserRepository(web_session).add(u)
    web_session.flush()
    patron = Patron(library_card_number="LINK0001", full_name="Link Test")
    SqlPatronRepository(web_session).add(patron)
    web_session.flush()

    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()

    # Link
    resp = web_client.post(
        f"/ui/patrons/LINK0001/link-user",
        data={"user_id": str(u.id), "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    web_session.refresh(patron)
    assert patron.user_id == u.id

    # Unlink
    raw2, signed2 = _make_csrf_pair()
    resp2 = web_client.post(
        f"/ui/patrons/LINK0001/unlink-user",
        data={"csrf_token": raw2},
        cookies={**auth_cookies, CSRF_COOKIE: signed2},
    )
    assert resp2.status_code == 303
    web_session.refresh(patron)
    assert patron.user_id is None


# ── Policy management ─────────────────────────────────────────────────────────


def test_policy_list_requires_auth(web_client):
    resp = web_client.get("/ui/policies")
    assert resp.status_code == 303
    assert "/ui/login" in resp.headers["location"]


def test_policy_list_renders_for_librarian(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/policies", cookies=cookies)
    assert resp.status_code == 200
    assert b"Loan Policies" in resp.content
    assert b"Default" in resp.content  # seed policy


def test_policy_new_form_renders(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/policies/new", cookies=cookies)
    assert resp.status_code == 200
    assert b"Create Loan Policy" in resp.content


def test_policy_create_redirects(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/policies/new",
        data={"name": "Test Policy", "loan_period_days": "7", "max_renewals": "1", "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert "/ui/policies" in resp.headers["location"]


def test_policy_update_persists(web_client, librarian, web_session):
    from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
    policies = SqlLoanPolicyRepository(web_session).list()
    default_policy = next(p for p in policies if p.is_default)

    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/policies/{default_policy.id}/update",
        data={
            "loan_period_days": "28",
            "max_renewals": "3",
            "is_default": "on",
            "csrf_token": raw,
        },
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    web_session.refresh(default_policy)
    assert default_policy.loan_period_days == 28
    assert default_policy.max_renewals == 3


def test_policy_create_as_default_swaps(web_client, librarian, web_session):
    from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
    repo = SqlLoanPolicyRepository(web_session)
    old_default = repo.get_default()
    assert old_default is not None

    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/policies/new",
        data={
            "name": "New Default",
            "loan_period_days": "30",
            "max_renewals": "0",
            "is_default": "on",
            "csrf_token": raw,
        },
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    web_session.refresh(old_default)
    assert old_default.is_default is False
    new_default = repo.get_default()
    assert new_default is not None
    assert new_default.name == "New Default"


# ── Role management ───────────────────────────────────────────────────────────


def test_role_list_requires_auth(web_client):
    resp = web_client.get("/ui/roles")
    assert resp.status_code == 303
    assert "/ui/login" in resp.headers["location"]


def test_role_list_renders_for_librarian(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/roles", cookies=cookies)
    assert resp.status_code == 200
    assert b"Roles" in resp.content
    assert b"Librarian" in resp.content


def test_role_new_form_renders(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/roles/new", cookies=cookies)
    assert resp.status_code == 200
    assert b"Create Role" in resp.content
    assert b"item.view" in resp.content  # permission picker populated


def test_role_create_with_permissions(web_client, librarian, web_session):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/roles/new",
        data={
            "name": "WebTestRole",
            "permissions": ["item.view", "loan.checkout"],
            "csrf_token": raw,
        },
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    from compendium.repositories.sql.role_repository import SqlRoleRepository
    role = SqlRoleRepository(web_session).get_by_name("WebTestRole")
    assert role is not None
    assert "item.view" in role.permissions
    assert role.is_system is False


def test_role_create_full_access(web_client, librarian, web_session):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/roles/new",
        data={"name": "FullRole", "full_access": "on", "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    from compendium.repositories.sql.role_repository import SqlRoleRepository
    role = SqlRoleRepository(web_session).get_by_name("FullRole")
    assert role is not None
    assert role.permissions == ["*"]


def test_role_detail_renders(web_client, librarian, web_session):
    from compendium.repositories.sql.role_repository import SqlRoleRepository
    patron_role = SqlRoleRepository(web_session).get_by_name("Patron")
    cookies = _login(web_client, "lib01")
    resp = web_client.get(f"/ui/roles/{patron_role.id}", cookies=cookies)
    assert resp.status_code == 200
    assert b"Patron" in resp.content


def test_role_detail_404(web_client, librarian):
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/roles/99999", cookies=cookies)
    assert resp.status_code == 404


def test_role_update_blocked_for_preset(web_client, librarian, web_session):
    from compendium.repositories.sql.role_repository import SqlRoleRepository
    lib_role = SqlRoleRepository(web_session).get_by_name("Librarian")
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/roles/{lib_role.id}/update",
        data={"name": "Librarian", "permissions": ["item.view"], "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


def test_role_clone_creates_editable_copy(web_client, librarian, web_session):
    from compendium.repositories.sql.role_repository import SqlRoleRepository
    read_only = SqlRoleRepository(web_session).get_by_name("ReadOnly")
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/roles/{read_only.id}/clone",
        data={"csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "/ui/roles/" in location
    new_id = int(location.rsplit("/", 1)[-1].split("?")[0])
    cloned = SqlRoleRepository(web_session).get(new_id)
    assert cloned is not None
    assert cloned.is_system is False
    assert cloned.name == "ReadOnly (copy)"
