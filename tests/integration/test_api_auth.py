"""Integration tests for auth endpoints and permission enforcement."""

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
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService, hash_password

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}

_TEST_SETTINGS = Settings(
    database_url="sqlite:///:memory:",
    jwt_secret_key="insecure-default-change-in-production",
)


@pytest.fixture(scope="module")
def api_engine():
    """Shared in-memory engine with StaticPool so all threads see the same DB."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def api_session(api_engine) -> Session:
    factory = sessionmaker(bind=api_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def api_client(api_engine, api_session):
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
    user = AppUser(
        username=username,
        email=None,
        password_hash=hash_password("password"),
        role_id=role.id,
    )
    SqlUserRepository(s).add(user)
    s.flush()
    user.role = role
    return user


def _token(s: Session, username: str, role_name: str) -> str:
    user = _make_user(s, username, role_name)
    return AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_TEST_SETTINGS,
    ).issue_token(user)


class TestLogin:
    def test_valid_login_returns_token(self, api_client, api_session):
        _make_user(api_session, "alice", "Librarian")
        resp = api_client.post("/auth/login", json={"username": "alice", "password": "password"})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_wrong_password_returns_401(self, api_client, api_session):
        _make_user(api_session, "bob", "ReadOnly")
        resp = api_client.post("/auth/login", json={"username": "bob", "password": "wrong"})
        assert resp.status_code == 401

    def test_unknown_user_returns_401(self, api_client, api_session):
        resp = api_client.post("/auth/login", json={"username": "nobody", "password": "pw"})
        assert resp.status_code == 401


class TestSearchWorks:
    def test_guest_can_search_when_enabled(self, api_client, api_session):
        resp = api_client.get("/works/search?q=Dune")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_search_returns_matching_work(self, api_client, api_session):
        with patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE):
            from compendium.repositories.sql.branch_repository import SqlBranchRepository
            from compendium.repositories.sql.creator_repository import SqlCreatorRepository
            from compendium.repositories.sql.item_repository import SqlItemRepository
            from compendium.repositories.sql.work_repository import SqlWorkRepository
            from compendium.services.catalog import CatalogService

            CatalogService(
                work_repo=SqlWorkRepository(api_session),
                item_repo=SqlItemRepository(api_session),
                creator_repo=SqlCreatorRepository(api_session),
                branch_repo=SqlBranchRepository(api_session),
            ).add_from_isbn("9780441013593")
            api_session.flush()

        resp = api_client.get("/works/search?q=Dune")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["title"] == "Dune"


class TestItemsEndpoint:
    def test_unauthenticated_returns_401(self, api_client, api_session):
        resp = api_client.get("/items/000001")
        assert resp.status_code == 401

    def test_authenticated_not_found_returns_404(self, api_client, api_session):
        token = _token(api_session, "librarian1", "Librarian")
        resp = api_client.get("/items/NOTREAL", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 404

    def test_readonly_user_can_view_item(self, api_client, api_session):
        token = _token(api_session, "reader1", "ReadOnly")
        resp = api_client.get("/items/NOTREAL", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 404


class TestPatronsEndpoint:
    def test_readonly_cannot_create_patron(self, api_client, api_session):
        token = _token(api_session, "reader2", "ReadOnly")
        resp = api_client.post(
            "/patrons",
            json={"full_name": "Test Patron"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_librarian_can_create_patron(self, api_client, api_session):
        token = _token(api_session, "librarian2", "Librarian")
        resp = api_client.post(
            "/patrons",
            json={"full_name": "Jane Doe"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["full_name"] == "Jane Doe"
        assert len(data["library_card_number"]) == 8
