"""Integration: Web UI routes for Household management."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base, Household, Patron
from compendium.repositories.sql.household_repository import SqlHouseholdRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from tests.helpers import setup_sqlite_fts, TEST_SECRET

_CSRF_KEY = _derive_csrf_secret(TEST_SECRET)


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
    username = f"web_lib_{id(db)}"
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


def _csrf_from_page(client: TestClient, path: str) -> str:
    """Fetch a page and extract the CSRF token."""
    r = client.get(path)
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    return m.group(1) if m else ""


def test_list_households_page(client, db):
    SqlHouseholdRepository(db).add(Household(name="Web Test HH"))
    db.commit()
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    r = client.get("/ui/households")
    assert r.status_code == 200
    assert "Web Test HH" in r.text


def test_new_household_form(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    r = client.get("/ui/households/new")
    assert r.status_code == 200
    assert "New Household" in r.text


def test_create_household_redirects_to_detail(client, db):
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    csrf_token = _csrf_from_page(client, "/ui/households/new")
    r = client.post(
        "/ui/households/new",
        data={"name": "Created via Web", "notes": "", "csrf_token": csrf_token},
    )
    assert r.status_code in (302, 303)
    assert "/ui/households/" in r.headers["location"]


def test_household_detail_shows_members(client, db):
    hh = SqlHouseholdRepository(db).add(Household(name="Detail Test HH"))
    p = Patron(library_card_number="WEB-MBR-002", full_name="Dave Detail")
    p.household_id = hh.id
    SqlPatronRepository(db).add(p)
    db.commit()
    username, pw = _make_librarian(db)
    _login(client, username, pw)
    r = client.get(f"/ui/households/{hh.id}")
    assert r.status_code == 200
    assert "Dave Detail" in r.text
