# tests/integration/test_web_patron_detail_household.py
"""Integration: Household section on patron detail page."""
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
def det_engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    setup_sqlite_fts(e)
    return e


@pytest.fixture
def db(det_engine):
    factory = sessionmaker(bind=det_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def client(det_engine, db):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=det_engine, autoflush=False, expire_on_commit=False)
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
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _CSRF_KEY)}"
    return raw, signed


def _make_librarian(db: Session) -> tuple[str, str]:
    role = SqlRoleRepository(db).get_by_name("Librarian")
    username = f"det_lib_{id(db)}"
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


def test_patron_edit_page_shows_prefilled_form(client, db):
    """GET /ui/patrons/{card}/edit renders 200 with the patron's name prefilled."""
    p = Patron(library_card_number="DET-010", full_name="Edit Me Patron")
    SqlPatronRepository(db).add(p)
    db.commit()

    username, pw = _make_librarian(db)
    _login(client, username, pw)
    r = client.get("/ui/patrons/DET-010/edit")
    assert r.status_code == 200
    assert 'value="Edit Me Patron"' in r.text


def test_patron_edit_full_form_persists_contact_fields(client, db):
    """POST with all fields (name/email/phone/category/expiry) updates the patron."""
    p = Patron(library_card_number="DET-011", full_name="Original Name")
    SqlPatronRepository(db).add(p)
    db.commit()

    username, pw = _make_librarian(db)
    _login(client, username, pw)
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/patrons/DET-011/edit",
        data={
            "full_name": "Renamed Patron",
            "contact_email": "renamed@example.com",
            "contact_phone": "555-0100",
            "category_id": "",
            "expires_at": "",
            "csrf_token": raw,
        },
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303

    page = client.get("/ui/patrons/DET-011")
    assert "Renamed Patron" in page.text
    assert "renamed@example.com" in page.text
    assert "555-0100" in page.text


def test_patron_edit_inline_category_only_regression(client, db):
    """The detail page's inline category/expiry form (no contact fields) still works."""
    p = Patron(
        library_card_number="DET-012",
        full_name="Inline Patron",
        contact_email="keep@example.com",
        contact_phone="555-0200",
    )
    SqlPatronRepository(db).add(p)
    db.commit()

    username, pw = _make_librarian(db)
    _login(client, username, pw)
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/patrons/DET-012/edit",
        data={
            "category_id": "",
            "expires_at": "2028-01-01",
            "csrf_token": raw,
        },
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303

    page = client.get("/ui/patrons/DET-012")
    assert "2028-01-01" in page.text
    # Contact fields must be preserved untouched since they weren't submitted.
    assert "keep@example.com" in page.text
    assert "555-0200" in page.text


def test_patron_detail_shows_household_section(client, db):
    """Patron detail shows household name and other members."""
    hh = SqlHouseholdRepository(db).add(Household(name="Detail Test HH"))
    p1 = Patron(library_card_number="DET-001", full_name="Parent One")
    p1.household_id = hh.id
    SqlPatronRepository(db).add(p1)
    p2 = Patron(library_card_number="DET-002", full_name="Child Two")
    p2.household_id = hh.id
    SqlPatronRepository(db).add(p2)
    db.commit()

    username, pw = _make_librarian(db)
    _login(client, username, pw)
    r = client.get("/ui/patrons/DET-001")
    assert r.status_code == 200
    assert "Detail Test HH" in r.text
    assert "Child Two" in r.text
    assert "DET-002" in r.text


def test_patron_detail_no_household_section_when_unlinked(client, db):
    """Patron detail shows no household section when patron has no household."""
    p = Patron(library_card_number="DET-003", full_name="Solo Patron")
    SqlPatronRepository(db).add(p)
    db.commit()

    username, pw = _make_librarian(db)
    _login(client, username, pw)
    r = client.get("/ui/patrons/DET-003")
    assert r.status_code == 200
    # No household heading present
    assert "Household:" not in r.text
