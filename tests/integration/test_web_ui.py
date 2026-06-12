"""Integration smoke tests for the web UI routes."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from tests.helpers import setup_sqlite_fts
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base, Item, Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.auth import hash_password
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
import compendium.services.site_settings as ss
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token

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
_CSRF_KEY = _derive_csrf_secret(_SECRET)


def _make_csrf_pair() -> tuple[str, str]:
    """Returns (raw_token, signed_cookie_value) for test requests."""
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _CSRF_KEY)}"
    return raw, signed


@pytest.fixture(scope="module")
def web_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)
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
    # Uses Administrator (wildcard) so existing tests pass through user/role/
    # system routes that slimmed Librarian no longer covers. Tests that need
    # to verify slimmed-Librarian denial behavior should create their own
    # role-bound user.
    role = SqlRoleRepository(web_session).get_by_name("Administrator")
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


def test_base_template_applies_default_theme(web_client):
    # Use catalog (full base layout with nav) — login is now a cover page with no nav band
    resp = web_client.get("/ui/catalog")
    assert b'data-theme="light"' in resp.content
    assert b"compendium_theme" in resp.content  # localStorage override script
    assert b'data-set-theme="auto"' in resp.content  # theme picker present


def test_base_template_favicon_and_brand_logo(web_client):
    # Use catalog (full base layout with nav) — login is now a cover page with no nav band
    resp = web_client.get("/ui/catalog")
    assert b'href="/ui/static/favicon.svg"' in resp.content
    assert b'brand-logo' in resp.content  # brand glyph carries brand-logo class


def test_base_template_auto_theme_omits_attribute(web_client, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_DEFAULT_THEME", "auto")
    resp = web_client.get("/ui/login")
    assert b"data-theme=" not in resp.content.split(b"</head>")[0]


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


def test_catalog_empty_query_browses_all_works(web_client, work):
    """Parity with CLI `item list`: visiting /ui/catalog with no query lists works."""
    resp = web_client.get("/ui/catalog")
    assert resp.status_code == 200
    assert b"Dune" in resp.content


def test_catalog_search_results_empty_query_lists_works(web_client, work):
    resp = web_client.get("/ui/catalog/search-results")
    assert resp.status_code == 200
    assert b"Dune" in resp.content


def test_catalog_detail_renders(web_client, work):
    w, _ = work
    resp = web_client.get(f"/ui/catalog/{w.id}")
    assert resp.status_code == 200
    assert b"Dune" in resp.content


def test_catalog_detail_shows_due_date_when_checked_out(web_client, work, web_session):
    from datetime import datetime, timezone

    from compendium.domain.enums import ItemStatus
    from compendium.domain.models import Loan, Patron

    w, item = work
    item.status = ItemStatus.CHECKED_OUT.value
    patron = Patron(library_card_number="DUE0001", full_name="Borrower")
    web_session.add(patron)
    web_session.flush()
    due = datetime(2099, 12, 31, tzinfo=timezone.utc)
    web_session.add(Loan(item_id=item.id, patron_id=patron.id, branch_id=item.branch_id, due_at=due))
    web_session.flush()

    resp = web_client.get(f"/ui/catalog/{w.id}")

    assert resp.status_code == 200
    assert b"due 2099-12-31" in resp.content


def test_catalog_detail_404(web_client):
    resp = web_client.get("/ui/catalog/99999")
    assert resp.status_code == 404


# ── Catalog suggest endpoint ──────────────────────────────────────────────────

_OPEN_LIB_FOUNDATION_SUGGEST = {
    "title": "Foundation",
    "authors": [{"name": "Isaac Asimov"}],
    "publishers": [{"name": "Gnome Press"}],
    "publish_date": "1951",
    "cover": {},
    "identifiers": {},
}
_ISBN_FOUNDATION_SUGGEST = "9780553293357"


@pytest.fixture
def foundation_work_for_suggest(web_session):
    with patch(
        "compendium.services.metadata.lookup_isbn",
        return_value=_OPEN_LIB_FOUNDATION_SUGGEST,
    ):
        w, _ = CatalogService(
            work_repo=SqlWorkRepository(web_session),
            item_repo=SqlItemRepository(web_session),
            creator_repo=SqlCreatorRepository(web_session),
            branch_repo=SqlBranchRepository(web_session),
            media_type_repo=SqlMediaTypeRepository(web_session),
        ).add_from_isbn(_ISBN_FOUNDATION_SUGGEST)
    web_session.flush()
    return w


def test_catalog_search_form_has_suggest_div(web_client):
    resp = web_client.get("/ui/catalog")
    assert resp.status_code == 200
    assert b'id="suggest-list"' in resp.content
    assert b'hx-get="/ui/catalog/suggest"' in resp.content


def test_suggest_endpoint_returns_partial_with_match(web_client, foundation_work_for_suggest):
    resp = web_client.get("/ui/catalog/suggest?q=foun")
    assert resp.status_code == 200
    assert b"suggest-options" in resp.content
    assert b"Foundation" in resp.content


def test_suggest_endpoint_short_query_returns_empty_body(web_client, foundation_work_for_suggest):
    resp = web_client.get("/ui/catalog/suggest?q=a")
    assert resp.status_code == 200
    assert b"suggest-options" not in resp.content


def test_suggest_endpoint_unauthenticated_blocked_when_guest_search_disabled(
    web_client, monkeypatch
):
    monkeypatch.setenv("COMPENDIUM_GUEST_SEARCH_ENABLED", "false")
    ss.invalidate_cache()
    try:
        resp = web_client.get("/ui/catalog/suggest?q=test")
        assert b"suggest-options" not in resp.content
    finally:
        ss.invalidate_cache()


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


def test_no_patron_account_page_has_working_logout(web_client, web_session):
    """Regression: logged-in user with no linked patron hits /ui/me/* → the error
    page must embed a valid CSRF token so its Logout form works."""
    import re

    role = SqlRoleRepository(web_session).get_by_name("Patron")
    user = AppUser(
        username="orphan01", password_hash=hash_password("secret"), role_id=role.id
    )
    SqlUserRepository(web_session).add(user)
    web_session.flush()

    _login(web_client, "orphan01")
    resp = web_client.get("/ui/me/loans")
    assert resp.status_code == 403
    assert b"No patron account" in resp.content

    match = re.search(rb'name="csrf_token"\s+value="([^"]+)"', resp.content)
    assert match is not None, "CSRF token input missing from page"
    raw = match.group(1).decode()
    assert raw, "CSRF token must not be empty"

    logout = web_client.post("/ui/logout", data={"csrf_token": raw})
    assert logout.status_code == 303


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


def test_librarian_places_hold_for_patron(web_client, librarian, patron_user, work):
    w, _ = work
    _, patron = patron_user
    cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/catalog/{w.id}/hold-for",
        data={"card_number": patron.library_card_number, "csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert b"Hold placed" in resp.content
    assert patron.library_card_number.encode() in resp.content


def test_patron_cannot_access_hold_for(web_client, patron_user, work):
    w, _ = work
    cookies = _login(web_client, "patron01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/catalog/{w.id}/hold-for",
        data={"card_number": "WEB0001", "csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 403


def test_librarian_cancels_patron_hold(web_client, librarian, patron_user, work, web_session):
    """Librarian views a patron and cancels one of their holds."""
    from compendium.repositories.sql.hold_repository import SqlHoldRepository

    w, _ = work
    _, patron = patron_user
    cookies_lib = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    # Librarian places a hold for the patron first (exercises P4 path).
    place = web_client.post(
        f"/ui/catalog/{w.id}/hold-for",
        data={"card_number": patron.library_card_number, "csrf_token": raw},
        cookies={**cookies_lib, CSRF_COOKIE: signed},
    )
    assert place.status_code == 200
    holds = SqlHoldRepository(web_session).get_active_for_patron(patron.id)
    assert len(holds) == 1
    hold_id = holds[0].id

    resp = web_client.post(
        f"/ui/patrons/{patron.library_card_number}/holds/{hold_id}/cancel",
        data={"csrf_token": raw},
        cookies={**cookies_lib, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert b"cancelled" in resp.content.lower()


def test_patron_loans_page_renders_for_librarian(web_client, librarian, patron_user):
    _, patron = patron_user
    cookies = _login(web_client, "lib01")
    resp = web_client.get(f"/ui/patrons/{patron.library_card_number}/loans", cookies=cookies)
    assert resp.status_code == 200
    assert b"Active loans" in resp.content
    assert patron.library_card_number.encode() in resp.content


def test_patron_loans_page_requires_patron_manage(web_client, patron_user):
    _, patron = patron_user
    cookies = _login(web_client, "patron01")
    resp = web_client.get(
        f"/ui/patrons/{patron.library_card_number}/loans",
        cookies=cookies,
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_patron_cannot_cancel_via_librarian_route(web_client, patron_user, work, web_session):
    from compendium.repositories.sql.hold_repository import SqlHoldRepository

    w, _ = work
    _, patron = patron_user
    cookies = _login(web_client, "patron01")
    raw, signed = _make_csrf_pair()
    web_client.post(
        f"/ui/catalog/{w.id}/hold",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    hold = SqlHoldRepository(web_session).get_active_for_patron(patron.id)[0]
    resp = web_client.post(
        f"/ui/patrons/{patron.library_card_number}/holds/{hold.id}/cancel",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 403


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


def test_work_edit_form_renders_for_librarian(web_client, librarian, work):
    w, _ = work
    cookies = _login(web_client, "lib01")
    resp = web_client.get(f"/ui/catalog/{w.id}/edit", cookies=cookies)
    assert resp.status_code == 200
    assert b"Edit work" in resp.content
    assert w.title.encode() in resp.content


def test_work_edit_form_denied_for_patron(web_client, patron_user, work):
    w, _ = work
    cookies = _login(web_client, "patron01")
    resp = web_client.get(f"/ui/catalog/{w.id}/edit", cookies=cookies)
    assert resp.status_code == 403


def test_work_edit_submit_updates_fields(web_client, librarian, work, web_session):
    w, _ = work
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/catalog/{w.id}/edit",
        data={
            "title": "Dune (Corrected)",
            "publisher": "Chilton",
            "publication_year": "1965",
            "description": "A spice-fuelled space epic.",
            "csrf_token": raw,
        },
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert f"/ui/catalog/{w.id}" in resp.headers["location"]
    assert "message=" in resp.headers["location"]

    refreshed = SqlWorkRepository(web_session).get(w.id)
    assert refreshed.title == "Dune (Corrected)"
    assert refreshed.publisher == "Chilton"
    assert refreshed.description == "A spice-fuelled space epic."


def test_work_edit_submit_empty_title_shows_error(web_client, librarian, work):
    w, _ = work
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/catalog/{w.id}/edit",
        data={"title": "   ", "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert b"Title is required" in resp.content


def test_work_edit_submit_bad_year_shows_error(web_client, librarian, work):
    w, _ = work
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/catalog/{w.id}/edit",
        data={"title": w.title, "publication_year": "not-a-year", "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert b"must be a number" in resp.content


def test_work_detail_shows_edit_button_for_librarian(web_client, librarian, work):
    w, _ = work
    cookies = _login(web_client, "lib01")
    resp = web_client.get(f"/ui/catalog/{w.id}", cookies=cookies)
    assert resp.status_code == 200
    assert f"/ui/catalog/{w.id}/edit".encode() in resp.content


def test_item_edit_form_renders_for_librarian(web_client, librarian, work):
    _, item = work
    cookies = _login(web_client, "lib01")
    resp = web_client.get(f"/ui/items/{item.barcode}/edit", cookies=cookies)
    assert resp.status_code == 200
    assert b"Shelf location" in resp.content
    assert b"Call number" in resp.content


def test_item_edit_form_denied_for_patron(web_client, patron_user, work):
    _, item = work
    cookies = _login(web_client, "patron01")
    resp = web_client.get(f"/ui/items/{item.barcode}/edit", cookies=cookies)
    assert resp.status_code == 403


def test_item_edit_submit_updates_fields(web_client, librarian, work, web_session):
    _, item = work
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/items/{item.barcode}/edit",
        data={
            "location": "Shelf Z",
            "call_number": "FIC HER",
            "condition": "worn",
            "notes": "cover repaired",
            "csrf_token": raw,
        },
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert f"/ui/items/{item.barcode}" in resp.headers["location"]
    assert "message=" in resp.headers["location"]

    refreshed = SqlItemRepository(web_session).get_by_barcode(item.barcode)
    assert refreshed.location == "Shelf Z"
    assert refreshed.call_number == "FIC HER"
    assert refreshed.condition == "worn"
    assert refreshed.notes == "cover repaired"


def test_item_edit_submit_unknown_barcode_404(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/items/NOSUCH/edit",
        data={"location": "X", "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 404


def test_item_detail_shows_edit_button_for_librarian(web_client, librarian, work):
    _, item = work
    cookies = _login(web_client, "lib01")
    resp = web_client.get(f"/ui/items/{item.barcode}", cookies=cookies)
    assert resp.status_code == 200
    assert f"/ui/items/{item.barcode}/edit".encode() in resp.content


# ── Creators editing ──────────────────────────────────────────────────────────


def test_work_creators_page_renders_for_librarian(web_client, librarian, work):
    w, _ = work
    cookies = _login(web_client, "lib01")
    resp = web_client.get(f"/ui/catalog/{w.id}/creators", cookies=cookies)
    assert resp.status_code == 200
    assert b"Manage creators" in resp.content
    assert b"Frank Herbert" in resp.content


def test_work_creators_page_denied_for_patron(web_client, patron_user, work):
    w, _ = work
    cookies = _login(web_client, "patron01")
    resp = web_client.get(f"/ui/catalog/{w.id}/creators", cookies=cookies)
    assert resp.status_code == 403


def test_work_creators_add(web_client, librarian, work, web_session):
    w, _ = work
    auth = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/catalog/{w.id}/creators/add",
        data={"name": "Kevin J. Anderson", "role": "author", "csrf_token": raw},
        cookies={**auth, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    refreshed = SqlWorkRepository(web_session).get(w.id)
    names = [wc.creator.display_name for wc in refreshed.creators]
    assert "Kevin J. Anderson" in names


def test_work_creators_remove(web_client, librarian, work, web_session):
    w, _ = work
    creator_id = w.creators[0].creator_id
    auth = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/catalog/{w.id}/creators/remove",
        data={"creator_id": str(creator_id), "role": "author", "csrf_token": raw},
        cookies={**auth, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    refreshed = SqlWorkRepository(web_session).get(w.id)
    assert refreshed.creators == []


def test_work_creators_move_down(web_client, librarian, work, web_session):
    w, _ = work
    svc = CatalogService(
        work_repo=SqlWorkRepository(web_session),
        item_repo=SqlItemRepository(web_session),
        creator_repo=SqlCreatorRepository(web_session),
        branch_repo=SqlBranchRepository(web_session),
        media_type_repo=SqlMediaTypeRepository(web_session),
    )
    svc.replace_creators(
        w.id, [("Frank Herbert", "author"), ("Brian Herbert", "author")]
    )
    web_session.flush()
    first_cid = w.creators[0].creator_id
    auth = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/catalog/{w.id}/creators/move",
        data={
            "creator_id": str(first_cid),
            "role": "author",
            "direction": "down",
            "csrf_token": raw,
        },
        cookies={**auth, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    refreshed = SqlWorkRepository(web_session).get(w.id)
    names = [wc.creator.display_name for wc in refreshed.creators]
    assert names == ["Brian Herbert", "Frank Herbert"]


def test_work_creators_add_bad_role_shows_error(web_client, librarian, work):
    w, _ = work
    auth = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/catalog/{w.id}/creators/add",
        data={"name": "Jane", "role": "bogus", "csrf_token": raw},
        cookies={**auth, CSRF_COOKIE: signed},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Unknown role" in resp.content


def test_creator_rename_form_renders_for_librarian(web_client, librarian, work):
    w, _ = work
    creator_id = w.creators[0].creator_id
    cookies = _login(web_client, "lib01")
    resp = web_client.get(f"/ui/creators/{creator_id}/edit", cookies=cookies)
    assert resp.status_code == 200
    assert b"Rename creator" in resp.content
    assert b"Frank Herbert" in resp.content


def test_creator_rename_submit_updates_everywhere(web_client, librarian, work, web_session):
    w, _ = work
    creator_id = w.creators[0].creator_id
    auth = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/creators/{creator_id}/edit",
        data={"display_name": "F. Herbert", "csrf_token": raw},
        cookies={**auth, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    refreshed = SqlWorkRepository(web_session).get(w.id)
    assert refreshed.creators[0].creator.display_name == "F. Herbert"


def test_work_detail_shows_manage_creators_link(web_client, librarian, work):
    w, _ = work
    cookies = _login(web_client, "lib01")
    resp = web_client.get(f"/ui/catalog/{w.id}", cookies=cookies)
    assert resp.status_code == 200
    assert f"/ui/catalog/{w.id}/creators".encode() in resp.content


def test_item_withdraw_via_web(web_client, librarian, work):
    _, item = work
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/items/{item.barcode}/withdraw",
        data={"csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert f"/ui/items/{item.barcode}" in resp.headers["location"]
    assert "message=" in resp.headers["location"]


def test_withdraw_confirm_page_renders(web_client, librarian, work):
    _, item = work
    auth_cookies = _login(web_client, "lib01")
    resp = web_client.get(
        f"/ui/items/{item.barcode}/withdraw-confirm",
        cookies=auth_cookies,
    )
    assert resp.status_code == 200
    assert b"Confirm withdraw" in resp.content


def test_withdraw_confirm_page_redirects_if_already_withdrawn(web_client, librarian, work):
    _, item = work
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    web_client.post(
        f"/ui/items/{item.barcode}/withdraw",
        data={"csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    resp = web_client.get(
        f"/ui/items/{item.barcode}/withdraw-confirm",
        cookies=auth_cookies,
    )
    assert resp.status_code == 303
    assert f"/ui/items/{item.barcode}" in resp.headers["location"]


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


# ── Book title search (Open Library) ─────────────────────────────────────────

_OL_SEARCH_RESPONSE = {
    "docs": [
        {
            "title": "Dune",
            "author_name": ["Frank Herbert"],
            "first_publish_year": 1965,
            "cover_i": 12345,
            "isbn": ["9780441013593", "0441013597"],
        },
        {
            "title": "Dune Messiah",
            "author_name": ["Frank Herbert"],
            "first_publish_year": 1969,
            "isbn": ["9780441172696"],
        },
        {
            "title": "No ISBN edition",
            "author_name": ["Obscure Author"],
            "isbn": [],
        },
    ]
}


def test_item_lookup_book_title_shows_candidates(web_client, librarian):
    """Book media type with a non-ISBN identifier triggers OL title search."""
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return _OL_SEARCH_RESPONSE

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            return _FakeResp()

    with patch("compendium.services.metadata.httpx.Client", _FakeClient):
        resp = web_client.post(
            "/ui/items/lookup",
            data={"media_type": "book", "identifier": "Dune", "csrf_token": raw},
            cookies={**auth_cookies, CSRF_COOKIE: signed},
        )
    assert resp.status_code == 200
    assert b"Dune" in resp.content
    assert b"Frank Herbert" in resp.content
    # Result without ISBN should be filtered out.
    assert b"Obscure Author" not in resp.content
    # Candidate button re-submits with the ISBN as identifier.
    assert b"9780441013593" in resp.content


# ── Music title search (MusicBrainz) ─────────────────────────────────────────

_MB_SEARCH_RESPONSE = {
    "releases": [
        {
            "id": "11111111-2222-3333-4444-555555555555",
            "title": "Kind of Blue",
            "date": "1959-08-17",
            "country": "US",
            "artist-credit": [{"artist": {"name": "Miles Davis"}}],
            "media": [{"format": "Vinyl"}],
        },
        {
            "id": "66666666-7777-8888-9999-aaaaaaaaaaaa",
            "title": "Kind of Blue (Reissue)",
            "date": "1997",
            "country": "GB",
            "artist-credit": [{"artist": {"name": "Miles Davis"}}],
            "media": [{"format": "CD"}],
        },
    ]
}


def test_item_lookup_vinyl_title_shows_candidates(web_client, librarian):
    """Vinyl media type with a non-UPC, non-MBID identifier triggers MB title search."""
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return _MB_SEARCH_RESPONSE

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            return _FakeResp()

    with patch("compendium.services.metadata.httpx.Client", _FakeClient):
        resp = web_client.post(
            "/ui/items/lookup",
            data={"media_type": "vinyl", "identifier": "Kind of Blue", "csrf_token": raw},
            cookies={**auth_cookies, CSRF_COOKIE: signed},
        )
    assert resp.status_code == 200
    assert b"Kind of Blue" in resp.content
    assert b"Miles Davis" in resp.content
    assert b"11111111-2222-3333-4444-555555555555" in resp.content
    assert b"Vinyl" in resp.content


# ── Manual item entry ─────────────────────────────────────────────────────────

def test_manual_add_form_renders(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/items/new/manual", cookies=auth_cookies)
    assert resp.status_code == 200
    assert b"Add Item Manually" in resp.content
    assert b'name="title"' in resp.content


def test_manual_add_creates_item_and_redirects(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/items/new/manual",
        data={
            "media_type": "book",
            "title": "Obscure Zine Issue 3",
            "authors": "Jane Doe, John Roe",
            "publisher": "Self-published",
            "year": "2020",
            "isbn": "",
            "upc": "",
            "description": "Not on Open Library.",
            "location": "Zine Shelf",
            "csrf_token": raw,
        },
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    # Redirects back to the manual form with success banner and the new barcode.
    location = resp.headers["location"]
    assert "/ui/items/new/manual" in location
    assert "added=" in location

    # Extract barcode and verify item was created.
    from urllib.parse import parse_qs, urlparse
    barcode = parse_qs(urlparse(location).query)["added"][0]
    detail = web_client.get(f"/ui/items/{barcode}", cookies=auth_cookies)
    assert detail.status_code == 200
    assert b"Obscure Zine Issue 3" in detail.content
    assert b"Zine Shelf" in detail.content


def test_manual_add_missing_title_shows_error(web_client, librarian):
    auth_cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/items/new/manual",
        data={"media_type": "book", "title": "", "csrf_token": raw},
        cookies={**auth_cookies, CSRF_COOKIE: signed},
    )
    # Empty title is blocked at the service layer via ValidationError.
    assert resp.status_code in (200, 422)


# ── Film (TMDb) item flow ─────────────────────────────────────────────────────

_TMDB_CANDIDATES = [
    {
        "identifier_value": "497",
        "title": "The Green Mile",
        "year": "1999",
        "secondary": "A supernatural tale.",
        "tertiary": "",
        "image_url": None,
        "tmdb_id": 497,
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


def test_patron_new_form_shows_create_login_section(web_client, librarian, web_session):
    # Librarians have patron.account.manage — they see the inline "create login" block
    cookies = _login(web_client, "lib01")
    resp = web_client.get("/ui/patrons/new", cookies=cookies)
    assert resp.status_code == 200
    assert b"Also create a login account" in resp.content
    assert b"create_username" in resp.content


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


# ── Security regressions ──────────────────────────────────────────────────────


def test_circ_checkout_escapes_barcode_and_card(web_client, librarian):
    """HTMLResponse error path must HTML-escape user-supplied input."""
    cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/circ/checkout",
        data={
            "barcode": "<script>alert('xss')</script>",
            "card_number": "<img src=x onerror=alert(1)>",
            "csrf_token": raw,
        },
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<script>" not in body
    assert "onerror=" not in body
    assert "&lt;script&gt;" in body or "&#x27;" in body or "&lt;img" in body


def test_me_renew_loan_escapes_error_message(web_client, patron_user):
    """Error branch returns HTMLResponse; make sure user input can't break HTML."""
    cookies = _login(web_client, "patron01")
    raw, signed = _make_csrf_pair()
    # Loan 999999 doesn't exist → NotFoundError → exc message goes into response.
    resp = web_client.post(
        "/ui/me/loans/999999/renew",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    # Error response must not contain a raw "<script>" tag from the exception text.
    assert "<script>" not in resp.text


def test_item_lookup_error_escapes_identifier(web_client, librarian):
    """If identifier triggers an error path, the echoed value must be escaped."""
    cookies = _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    # Supply an invalid ISBN containing HTML; the detect_kind / normalize_isbn path
    # raises ValidationError which is echoed back via HTMLResponse.
    resp = web_client.post(
        "/ui/items/lookup",
        data={
            "media_type": "book",
            "identifier": "<script>x</script>",
            "csrf_token": raw,
        },
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert "<script>x</script>" not in resp.text


def test_open_redirect_on_login_falls_back_to_catalog(web_client, librarian):
    """next= must only accept /ui/ paths; protocol-relative URLs fall back."""
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/login?next=//evil.example.com/path",
        data={"username": "lib01", "password": "secret", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/ui/catalog")


def test_open_redirect_absolute_url_falls_back(web_client, librarian):
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/login?next=https://evil.example.com/",
        data={"username": "lib01", "password": "secret", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/ui/catalog")


@pytest.mark.parametrize("bad_next", [
    "//evil.com",
    "/\\evil.com",
    "/ui/%5C%5Cevil.com",  # URL-encoded backslashes
    "http://evil.com/ui/ok",
    "//evil.com/ui/ok",
])
def test_open_redirect_parametrized_bad_values_fall_back(web_client, librarian, bad_next):
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        f"/ui/login?next={bad_next}",
        data={"username": "lib01", "password": "secret", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/ui/catalog"), f"Expected fallback for next={bad_next!r}"


def test_open_redirect_valid_ui_path_allowed(web_client, librarian):
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/login?next=/ui/catalog",
        data={"username": "lib01", "password": "secret", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/catalog"


def test_patron_sees_403_not_login_redirect_on_librarian_page(web_client, patron_user):
    """A logged-in user without the permission should get 403, not a login loop."""
    cookies = _login(web_client, "patron01")
    resp = web_client.get("/ui/circ", cookies=cookies)
    assert resp.status_code == 403


def test_csrf_signature_tamper_rejected(web_client, librarian):
    """Tampering with the signed cookie (not just the raw token) must fail."""
    cookies = _login(web_client, "lib01")
    raw = generate_token()
    # Sign with the wrong secret (raw bytes — _sign expects bytes now).
    bad_signed = f"{raw}.{_sign(raw, b'wrong-secret')}"
    resp = web_client.post(
        "/ui/circ/checkout",
        data={"barcode": "X", "card_number": "Y", "csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: bad_signed},
    )
    assert resp.status_code == 403


# ── Password change / reset ───────────────────────────────────────────────────


AUTH_COOKIE = "compendium_auth"


def test_me_password_form_renders(web_client, patron_user):
    _login(web_client, "patron01")
    resp = web_client.get("/ui/me/password")
    assert resp.status_code == 200
    assert b"Change Password" in resp.content


def test_me_password_change_succeeds(web_client, patron_user):
    _login(web_client, "patron01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/me/password",
        data={
            "current_password": "secret",
            "new_password": "newsecret",
            "confirm_password": "newsecret",
            "csrf_token": raw,
        },
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert "/ui/login" in resp.headers["location"]
    # Auth cookie was cleared so the user is logged out.
    set_cookie = " ".join(resp.headers.get_list("set-cookie"))
    assert AUTH_COOKIE in set_cookie
    # Old password no longer authenticates.
    raw2, signed2 = _make_csrf_pair()
    bad = web_client.post(
        "/ui/login",
        data={"username": "patron01", "password": "secret", "csrf_token": raw2},
        cookies={CSRF_COOKIE: signed2},
    )
    assert bad.status_code == 401
    # New password works.
    raw3, signed3 = _make_csrf_pair()
    good = web_client.post(
        "/ui/login",
        data={"username": "patron01", "password": "newsecret", "csrf_token": raw3},
        cookies={CSRF_COOKIE: signed3},
    )
    assert good.status_code == 303


def test_me_password_wrong_current_rejected(web_client, patron_user):
    _login(web_client, "patron01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/me/password",
        data={
            "current_password": "wrong",
            "new_password": "newsecret",
            "confirm_password": "newsecret",
            "csrf_token": raw,
        },
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 401
    assert b"Current password is incorrect" in resp.content


def test_me_password_mismatched_confirmation_rejected(web_client, patron_user):
    _login(web_client, "patron01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/me/password",
        data={
            "current_password": "secret",
            "new_password": "newsecret",
            "confirm_password": "different",
            "csrf_token": raw,
        },
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 400
    assert b"do not match" in resp.content


def test_admin_reset_password_succeeds(web_client, librarian, patron_user):
    _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/users/patron01/reset-password",
        data={
            "actor_current_password": "secret",  # librarian's password
            "new_password": "forced-new",
            "confirm_password": "forced-new",
            "csrf_token": raw,
        },
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert "message=Password" in resp.headers["location"]
    # patron01 can now log in with the reset password.
    raw2, signed2 = _make_csrf_pair()
    login = web_client.post(
        "/ui/login",
        data={"username": "patron01", "password": "forced-new", "csrf_token": raw2},
        cookies={CSRF_COOKIE: signed2},
    )
    assert login.status_code == 303


def test_admin_reset_wrong_actor_password_rejected(web_client, librarian, patron_user):
    _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/users/patron01/reset-password",
        data={
            "actor_current_password": "wrong",
            "new_password": "forced-new",
            "confirm_password": "forced-new",
            "csrf_token": raw,
        },
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    # Patron's original password still works.
    raw2, signed2 = _make_csrf_pair()
    login = web_client.post(
        "/ui/login",
        data={"username": "patron01", "password": "secret", "csrf_token": raw2},
        cookies={CSRF_COOKIE: signed2},
    )
    assert login.status_code == 303


def test_admin_reset_self_redirects_to_self_service(web_client, librarian):
    _login(web_client, "lib01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/users/lib01/reset-password",
        data={
            "actor_current_password": "secret",
            "new_password": "newsecret",
            "confirm_password": "newsecret",
            "csrf_token": raw,
        },
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/me/password"


def test_patron_cannot_reset_other_user_password(web_client, patron_user, librarian):
    _login(web_client, "patron01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/users/lib01/reset-password",
        data={
            "actor_current_password": "secret",
            "new_password": "hax",
            "confirm_password": "hax",
            "csrf_token": raw,
        },
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 403


def test_inactive_user_cookie_denied(web_client, web_session, librarian):
    """An inactive user's still-valid auth cookie must not grant access."""
    cookies = _login(web_client, "lib01")
    librarian.is_active = False
    SqlUserRepository(web_session).update(librarian)
    web_session.commit()
    resp = web_client.get("/ui/circ", cookies=cookies)
    # get_web_user returns None for inactive users → RequiresLoginException.
    assert resp.status_code == 303
    assert "/ui/login" in resp.headers["location"]


# ── UI polish — Slice A (catalog landing) ─────────────────────────────────────


def test_search_landing_renders_shelf_author(web_client, work):
    """New-arrivals shelf renders the creator byline after the N+1 fix."""
    resp = web_client.get("/ui/catalog")
    assert resp.status_code == 200
    assert b"shelf-creator" in resp.content
    assert b"Frank Herbert" in resp.content


def test_search_landing_marks_coverless_thumbs_empty(web_client, work):
    """Works with no cover image get the cover-thumb-empty modifier and data-media-type."""
    # The seeded work (_OPEN_LIB_DUNE) has no cover (cover: {}), so cover_image_url is None.
    resp = web_client.get("/ui/catalog")
    assert resp.status_code == 200
    assert b"cover-thumb-empty" in resp.content
    assert b"data-media-type=" in resp.content


def test_facet_drawer_summary_present(web_client, work):
    """The catalog search-results page renders <summary>Filter</summary> for the mobile facet toggle."""
    resp = web_client.get("/ui/catalog?q=the")
    assert resp.status_code == 200
    assert b"<summary>Filter</summary>" in resp.content


# ── ISBN/UPC circulation fallback ─────────────────────────────────────────────


def test_desk_checkout_by_isbn(web_client, web_session, work):
    _, item = work
    role = SqlRoleRepository(web_session).get_by_name("Administrator")
    lib = AppUser(
        username="lib_isbn01", password_hash=hash_password("secret"), role_id=role.id
    )
    SqlUserRepository(web_session).add(lib)
    p = Patron(library_card_number="ISBNWEB01", full_name="Isbn Patron")
    SqlPatronRepository(web_session).add(p)
    web_session.flush()
    cookies = _login(web_client, "lib_isbn01")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/circ/checkout",
        data={"barcode": _ISBN, "card_number": "ISBNWEB01", "csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert b"Checked out" in resp.content
    assert item.status == "checked_out"


def test_desk_checkin_ambiguous_isbn_shows_picker(web_client, web_session, work):
    w, item1 = work
    role = SqlRoleRepository(web_session).get_by_name("Administrator")
    u = AppUser(username="lib_isbn02", password_hash=hash_password("secret"), role_id=role.id)
    SqlUserRepository(web_session).add(u)
    branch = SqlBranchRepository(web_session).get_default()
    item2 = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode="AMBIG-2",
        accession_number="AMBIG-A2",
    )
    SqlItemRepository(web_session).add(item2)
    p1 = Patron(library_card_number="AMBIG01", full_name="Amber One")
    p2 = Patron(library_card_number="AMBIG02", full_name="Amber Two")
    SqlPatronRepository(web_session).add(p1)
    SqlPatronRepository(web_session).add(p2)
    web_session.flush()
    circ = CirculationService(
        item_repo=SqlItemRepository(web_session),
        loan_repo=SqlLoanRepository(web_session),
        patron_repo=SqlPatronRepository(web_session),
        branch_repo=SqlBranchRepository(web_session),
        hold_repo=SqlHoldRepository(web_session),
        policy_repo=SqlLoanPolicyRepository(web_session),
    )
    circ.checkout(item1.barcode, "AMBIG01")
    circ.checkout("AMBIG-2", "AMBIG02")
    web_session.flush()

    cookies = _login(web_client, "lib_isbn02")
    raw, signed = _make_csrf_pair()
    resp = web_client.post(
        "/ui/circ/checkin",
        data={"barcode": _ISBN, "csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert b"Which copy came back?" in resp.content
    assert b"Amber One" in resp.content
    assert b"Amber Two" in resp.content
    assert item1.barcode.encode() in resp.content
    assert b"AMBIG-2" in resp.content

    # Click-through: re-posting a picker button's embedded barcode completes
    # the checkin (CSRF token is reusable within the cookie's validity).
    resp = web_client.post(
        "/ui/circ/checkin",
        data={"barcode": item1.barcode, "csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert b"Checked in" in resp.content
