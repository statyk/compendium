# tests/integration/test_api_households.py
"""Integration: REST API endpoints for Household management."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base, Household, Patron
from compendium.repositories.sql.household_repository import SqlHouseholdRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService, hash_password
from tests.helpers import setup_sqlite_fts

_TEST_SETTINGS = Settings(
    database_url="sqlite:///:memory:",
    jwt_secret_key="insecure-default-change-in-production",
)


def _issue_token(s: Session, user: AppUser) -> str:
    return AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_TEST_SETTINGS,
    ).issue_token(user)


@pytest.fixture(scope="module")
def api_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)
    return engine


@pytest.fixture
def db(api_engine) -> Session:
    factory = sessionmaker(bind=api_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def client(api_engine, db):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=api_engine, autoflush=False, expire_on_commit=False)
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
    return TestClient(app)


def _make_user(s: Session, username: str, role_name: str) -> AppUser:
    role = SqlRoleRepository(s).get_by_name(role_name)
    u = AppUser(username=username, password_hash=hash_password("Str0ng!Pass"), role_id=role.id)
    u = SqlUserRepository(s).add(u)
    s.flush()
    u.role = role
    return u


def _auth_header(s: Session, username: str, role_name: str) -> dict:
    user = _make_user(s, username, role_name)
    s.commit()
    token = _issue_token(s, user)
    return {"Authorization": f"Bearer {token}"}


def _make_patron(s: Session, card: str, name: str, household_id=None) -> Patron:
    p = Patron(
        library_card_number=card,
        full_name=name,
        household_id=household_id,
    )
    return SqlPatronRepository(s).add(p)


class TestCreateHousehold:
    def test_librarian_can_create(self, client, db):
        headers = _auth_header(db, "lib1", "Librarian")
        r = client.post(
            "/households",
            json={"name": "Smith Family"},
            headers=headers,
        )
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "Smith Family"
        assert data["id"] > 0

    def test_patron_role_forbidden(self, client, db):
        headers = _auth_header(db, "p1", "Patron")
        r = client.post(
            "/households",
            json={"name": "Test"},
            headers=headers,
        )
        assert r.status_code == 403

    def test_blank_name_returns_422(self, client, db):
        headers = _auth_header(db, "lib2", "Librarian")
        r = client.post(
            "/households",
            json={"name": "  "},
            headers=headers,
        )
        assert r.status_code == 422


class TestGetHousehold:
    def test_get_by_id(self, client, db):
        hh = SqlHouseholdRepository(db).add(Household(name="Jones Family"))
        db.commit()
        headers = _auth_header(db, "lib3", "Librarian")
        r = client.get(f"/households/{hh.id}", headers=headers)
        assert r.status_code == 200
        assert r.json()["name"] == "Jones Family"

    def test_not_found_returns_404(self, client, db):
        headers = _auth_header(db, "lib4", "Librarian")
        r = client.get("/households/99999", headers=headers)
        assert r.status_code == 404


class TestListHouseholds:
    def test_returns_list(self, client, db):
        SqlHouseholdRepository(db).add(Household(name="Alpha House"))
        db.commit()
        headers = _auth_header(db, "lib5", "Librarian")
        r = client.get("/households", headers=headers)
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        assert any(h["name"] == "Alpha House" for h in r.json())


class TestUpdateHousehold:
    def test_patch_name(self, client, db):
        hh = SqlHouseholdRepository(db).add(Household(name="Old Name"))
        db.commit()
        headers = _auth_header(db, "lib6", "Librarian")
        r = client.patch(
            f"/households/{hh.id}",
            json={"name": "New Name"},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["name"] == "New Name"


class TestDeleteHousehold:
    def test_delete_empty_household(self, client, db):
        hh = SqlHouseholdRepository(db).add(Household(name="To Delete"))
        hh_id = hh.id
        db.commit()
        headers = _auth_header(db, "lib7", "Librarian")
        r = client.delete(f"/households/{hh_id}", headers=headers)
        assert r.status_code == 204

    def test_delete_with_members_returns_422(self, client, db):
        hh = SqlHouseholdRepository(db).add(Household(name="Has Members"))
        _make_patron(db, "HH-API-001", "Alice", household_id=hh.id)
        db.commit()
        headers = _auth_header(db, "lib8", "Librarian")
        r = client.delete(f"/households/{hh.id}", headers=headers)
        assert r.status_code == 422


class TestMemberManagement:
    def test_add_member(self, client, db):
        hh = SqlHouseholdRepository(db).add(Household(name="Member Test HH"))
        _make_patron(db, "HH-API-002", "Bob")
        db.commit()
        headers = _auth_header(db, "lib9", "Librarian")
        r = client.post(
            f"/households/{hh.id}/members",
            json={"card_number": "HH-API-002"},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["household_id"] == hh.id

    def test_remove_member(self, client, db):
        hh = SqlHouseholdRepository(db).add(Household(name="Remove Test HH"))
        patron = _make_patron(db, "HH-API-003", "Carol", household_id=None)
        db.commit()
        patron.household_id = hh.id
        SqlPatronRepository(db).update(patron)
        db.commit()
        headers = _auth_header(db, "lib10", "Librarian")
        r = client.delete(
            f"/households/{hh.id}/members/HH-API-003",
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["household_id"] is None
