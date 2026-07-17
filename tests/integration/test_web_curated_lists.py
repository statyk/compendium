"""Integration tests for Curated Lists web UI routes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base, MediaType, Work
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from tests.helpers import setup_sqlite_fts, TEST_SECRET

_CSRF_KEY = _derive_csrf_secret(TEST_SECRET)

_counter = {"n": 0}


def _next() -> int:
    _counter["n"] += 1
    return _counter["n"]


@pytest.fixture(scope="module")
def web_engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    setup_sqlite_fts(e)
    return e


@pytest.fixture
def db(web_engine):
    factory = sessionmaker(bind=web_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def client(web_engine, db):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=web_engine, autoflush=False, expire_on_commit=False)
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
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _make_csrf_pair() -> tuple[str, str]:
    """Return (raw_token, signed_cookie_value) for test requests."""
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _CSRF_KEY)}"
    return raw, signed


def _make_librarian(db: Session) -> tuple[str, str]:
    """Create a librarian user; return (username, password)."""
    role = SqlRoleRepository(db).get_by_name("Librarian")
    username = f"cl_lib_{_next()}"
    u = AppUser(username=username, password_hash=hash_password("Str0ng!Pass"), role_id=role.id)
    SqlUserRepository(db).add(u)
    db.commit()
    return username, "Str0ng!Pass"


def _make_readonly(db: Session) -> tuple[str, str]:
    """Create a ReadOnly user; return (username, password)."""
    role = SqlRoleRepository(db).get_by_name("ReadOnly")
    username = f"cl_ro_{_next()}"
    u = AppUser(username=username, password_hash=hash_password("Str0ng!Pass"), role_id=role.id)
    SqlUserRepository(db).add(u)
    db.commit()
    return username, "Str0ng!Pass"


def _login(client: TestClient, username: str, password: str) -> None:
    """Log in via /ui/login; cookies are persisted on the client instance."""
    raw, signed = _make_csrf_pair()
    r = client.post(
        "/ui/login",
        data={"username": username, "password": password, "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert r.status_code == 303, f"Login failed: {r.status_code}"


def _add_work(db: Session, *, title: str = "Test Work") -> Work:
    """Insert a Work directly into the DB and return it."""
    mt = db.query(MediaType).filter_by(code="book").one()
    n = _next()
    w = Work(
        title=title,
        media_type_id=mt.id,
        search_text=title,
    )
    db.add(w)
    db.flush()
    return w


def _create_list_via_web(client: TestClient, name: str, **kwargs) -> str:
    """Create a curated list via POST and return its slug (from redirect Location)."""
    raw, signed = _make_csrf_pair()
    data = {"name": name, "description": "", "csrf_token": raw}
    data.update(kwargs)
    r = client.post(
        "/ui/curated-lists/new",
        data=data,
        cookies={CSRF_COOKIE: signed},
    )
    assert r.status_code == 303, f"Expected 303 creating list, got {r.status_code}: {r.text[:200]}"
    # Location is /ui/curated-lists/{slug}
    slug = r.headers["location"].split("/ui/curated-lists/")[1]
    return slug


# ---------------------------------------------------------------------------
# Group 1: Admin CRUD (logged in as Librarian)
# ---------------------------------------------------------------------------


def test_list_page_empty(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    r = client.get("/ui/curated-lists")
    assert r.status_code == 200
    assert "No curated lists" in r.text


def test_create_list(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    raw, signed = _make_csrf_pair()
    r = client.post(
        "/ui/curated-lists/new",
        data={"name": "My New List", "description": "A test list", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert r.status_code == 303
    assert "/ui/curated-lists/" in r.headers["location"]


def test_curated_list_status_uses_css_classes(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    slug = _create_list_via_web(client, "CSS Class Status List")
    r = client.get("/ui/curated-lists")
    assert r.status_code == 200
    assert 'class="status-public"' in r.text or 'class="status-private"' in r.text
    assert 'style="color:#2d7a2d"' not in r.text

    r_detail = client.get(f"/ui/curated-lists/{slug}")
    assert r_detail.status_code == 200
    assert 'class="status-public"' in r_detail.text or 'class="status-private"' in r_detail.text
    assert 'style="color:#2d7a2d"' not in r_detail.text
    assert 'style="color:#888"' not in r_detail.text


def test_create_list_blank_name(client, db):
    """A whitespace-only name passes FastAPI's form parsing but fails service validation."""
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    raw, signed = _make_csrf_pair()
    # HTTPX drops empty strings from form data, so send whitespace to reach the handler.
    r = client.post(
        "/ui/curated-lists/new",
        data={"name": "   ", "description": "", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert r.status_code == 200


def test_detail_page(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    slug = _create_list_via_web(client, "Detail Page List")
    r = client.get(f"/ui/curated-lists/{slug}")
    assert r.status_code == 200
    assert "Detail Page List" in r.text


def test_edit_list(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    slug = _create_list_via_web(client, "Edit Source List")
    raw, signed = _make_csrf_pair()
    r = client.post(
        f"/ui/curated-lists/{slug}/edit",
        data={
            "name": "Edited List Name",
            "description": "",
            "is_public": "1",
            "display_order": "0",
            "new_slug": "",
            "csrf_token": raw,
        },
        cookies={CSRF_COOKIE: signed},
    )
    assert r.status_code == 303


def test_delete_list(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    slug = _create_list_via_web(client, "List To Delete")
    raw, signed = _make_csrf_pair()
    r = client.post(
        f"/ui/curated-lists/{slug}/delete",
        data={"csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("/ui/curated-lists") or "/ui/curated-lists" in r.headers["location"]


def test_add_work(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    work = _add_work(db, title="Work For List")
    db.commit()
    slug = _create_list_via_web(client, "List With Work")
    raw, signed = _make_csrf_pair()
    r = client.post(
        f"/ui/curated-lists/{slug}/works/add",
        data={"work_id": str(work.id), "annotation": "", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert r.status_code == 303
    assert f"/ui/curated-lists/{slug}" in r.headers["location"]


def test_add_work_duplicate(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    work = _add_work(db, title="Duplicate Work")
    db.commit()
    slug = _create_list_via_web(client, "List Dup Test")
    # First add
    raw, signed = _make_csrf_pair()
    r = client.post(
        f"/ui/curated-lists/{slug}/works/add",
        data={"work_id": str(work.id), "annotation": "", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert r.status_code == 303
    # Second add (duplicate) — should redirect with error param
    raw2, signed2 = _make_csrf_pair()
    r2 = client.post(
        f"/ui/curated-lists/{slug}/works/add",
        data={"work_id": str(work.id), "annotation": "", "csrf_token": raw2},
        cookies={CSRF_COOKIE: signed2},
    )
    # Route redirects back with ?error= on duplicate
    assert r2.status_code == 303
    assert "error" in r2.headers["location"]


def test_remove_work(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    work = _add_work(db, title="Remove Me Work")
    db.commit()
    slug = _create_list_via_web(client, "List Remove Work")
    # Add the work
    raw, signed = _make_csrf_pair()
    client.post(
        f"/ui/curated-lists/{slug}/works/add",
        data={"work_id": str(work.id), "annotation": "", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    # Remove the work
    raw2, signed2 = _make_csrf_pair()
    r = client.post(
        f"/ui/curated-lists/{slug}/works/{work.id}/remove",
        data={"csrf_token": raw2},
        cookies={CSRF_COOKIE: signed2},
    )
    assert r.status_code == 303
    assert f"/ui/curated-lists/{slug}" in r.headers["location"]


def test_reorder_works(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    work1 = _add_work(db, title="Reorder Work A")
    work2 = _add_work(db, title="Reorder Work B")
    db.commit()
    slug = _create_list_via_web(client, "List Reorder Test")
    # Add both works
    for w in (work1, work2):
        raw, signed = _make_csrf_pair()
        client.post(
            f"/ui/curated-lists/{slug}/works/add",
            data={"work_id": str(w.id), "annotation": "", "csrf_token": raw},
            cookies={CSRF_COOKIE: signed},
        )
    # Reorder with reversed order
    raw3, signed3 = _make_csrf_pair()
    r = client.post(
        f"/ui/curated-lists/{slug}/works/reorder",
        data={"work_order": f"{work2.id},{work1.id}", "csrf_token": raw3},
        cookies={CSRF_COOKIE: signed3},
    )
    assert r.status_code == 303
    assert f"/ui/curated-lists/{slug}" in r.headers["location"]


# ---------------------------------------------------------------------------
# Group 2: Permission enforcement
# ---------------------------------------------------------------------------


def test_admin_routes_require_permission(client, db):
    username, pw = _make_readonly(db)
    _login(client, username, pw)
    r = client.get("/ui/curated-lists")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Group 3: Public views
# ---------------------------------------------------------------------------


def test_public_list_index(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    _create_list_via_web(client, "Public Visible List", is_public="1")
    # Request as anonymous (no auth cookie on a fresh client via separate call)
    r = client.get("/ui/lists")
    assert r.status_code == 200
    assert "Public Visible List" in r.text


def test_private_list_hidden_from_index(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    # Create a private list (no is_public field = falsy)
    raw, signed = _make_csrf_pair()
    r = client.post(
        "/ui/curated-lists/new",
        data={"name": "Private Hidden List", "description": "", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert r.status_code == 303
    # /ui/lists only returns public lists
    r2 = client.get("/ui/lists")
    assert r2.status_code == 200
    assert "Private Hidden List" not in r2.text


def test_public_list_view(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    slug = _create_list_via_web(client, "Public View List", is_public="1")
    r = client.get(f"/ui/lists/{slug}")
    assert r.status_code == 200
    assert "Public View List" in r.text


def test_private_list_404_for_guest(client, db, web_engine):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    # Create private list (no is_public — omit the checkbox entirely)
    raw, signed = _make_csrf_pair()
    r = client.post(
        "/ui/curated-lists/new",
        data={"name": "Private 404 List", "description": "", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert r.status_code == 303
    slug = r.headers["location"].split("/ui/curated-lists/")[1]
    # Verify the list is private by checking its detail page shows "Private"
    r_detail = client.get(f"/ui/curated-lists/{slug}")
    assert "Private" in r_detail.text

    # Access as anonymous (guest) with a fresh unauthenticated client
    app_guest = create_app()

    def _guest_override():
        factory = sessionmaker(bind=web_engine, autoflush=False, expire_on_commit=False)
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app_guest.dependency_overrides[get_session] = _guest_override
    with TestClient(app_guest, follow_redirects=False) as guest:
        r2 = guest.get(f"/ui/lists/{slug}")
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Group 4: Landing shelf
# ---------------------------------------------------------------------------


def test_featured_list_on_landing(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    slug = _create_list_via_web(
        client, "Featured Landing List", is_public="1", is_featured="1"
    )
    r = client.get("/ui/catalog")
    assert r.status_code == 200
    assert "Featured Landing List" in r.text


def test_non_featured_list_not_on_landing(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    _create_list_via_web(client, "NonFeatured Check List", is_public="1")
    # This list has is_featured=False, should not appear in featured shelves on landing
    r = client.get("/ui/catalog")
    assert r.status_code == 200
    # The list name might appear in the nav or elsewhere — we verify it's not
    # in the discovery-shelves section by checking the featured_lists context
    # (the template only renders featured lists in the shelf section).
    # The name should NOT appear since it's not featured and not in search results.
    assert "NonFeatured Check List" not in r.text
