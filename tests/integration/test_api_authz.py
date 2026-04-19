"""Regression tests for authorization boundaries on legacy /holds endpoints,
inactive-user token handling, and self-deactivation prevention."""

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
from compendium.services.auth import AuthService, hash_password
from compendium.services.catalog import CatalogService
from tests.helpers import setup_sqlite_fts

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}

_SETTINGS = Settings(
    database_url="sqlite:///:memory:",
    jwt_secret_key="insecure-default-change-in-production",
)


@pytest.fixture(scope="module")
def authz_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)
    return engine


@pytest.fixture
def authz_session(authz_engine) -> Session:
    factory = sessionmaker(bind=authz_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def authz_client(authz_engine, authz_session):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=authz_engine, autoflush=False, expire_on_commit=False)
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


_counter = 0


def _make_patron_user(s: Session, username: str) -> tuple[AppUser, Patron, str]:
    global _counter
    _counter += 1
    role = SqlRoleRepository(s).get_by_name("Patron")
    user = AppUser(username=username, password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(s).add(user)
    s.flush()
    user.role = role
    patron = Patron(
        library_card_number=f"AZ{_counter:04d}",
        full_name=f"Patron {_counter}",
        user_id=user.id,
    )
    SqlPatronRepository(s).add(patron)
    s.flush()
    token = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(user)
    return user, patron, token


def _make_librarian(s: Session, username: str) -> tuple[AppUser, str]:
    role = SqlRoleRepository(s).get_by_name("Librarian")
    user = AppUser(username=username, password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(s).add(user)
    s.flush()
    user.role = role
    token = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(user)
    return user, token


def _catalog(s: Session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(s),
        item_repo=SqlItemRepository(s),
        creator_repo=SqlCreatorRepository(s),
        branch_repo=SqlBranchRepository(s),
        media_type_repo=SqlMediaTypeRepository(s),
    )


class TestLegacyHoldsIDOR:
    """Verify the /holds API enforces ownership for .self-scoped callers."""

    def test_patron_cannot_place_hold_for_other_patron(self, authz_client, authz_session):
        _, patron_a, _ = _make_patron_user(authz_session, "idor_place_a")
        _, _, token_b = _make_patron_user(authz_session, "idor_place_b")
        with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
            work, _ = _catalog(authz_session).add_from_isbn("9780441090001")
        authz_session.flush()

        resp = authz_client.post(
            "/holds/",
            json={"work_id": work.id, "card_number": patron_a.library_card_number},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 403

    def test_patron_cannot_list_other_patrons_holds(self, authz_client, authz_session):
        _, patron_a, _ = _make_patron_user(authz_session, "idor_list_a")
        _, _, token_b = _make_patron_user(authz_session, "idor_list_b")
        resp = authz_client.get(
            "/holds/",
            params={"card_number": patron_a.library_card_number},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 403

    def test_patron_cannot_cancel_other_patrons_hold(self, authz_client, authz_session):
        _, patron_a, token_a = _make_patron_user(authz_session, "idor_cancel_a")
        _, _, token_b = _make_patron_user(authz_session, "idor_cancel_b")
        with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
            work, _ = _catalog(authz_session).add_from_isbn("9780441090002")
        authz_session.flush()

        place = authz_client.post(
            "/holds/",
            json={"work_id": work.id, "card_number": patron_a.library_card_number},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert place.status_code == 201
        hold_id = place.json()["id"]

        resp = authz_client.delete(
            f"/holds/{hold_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 403

    def test_patron_can_still_operate_on_own_resources(self, authz_client, authz_session):
        _, patron, token = _make_patron_user(authz_session, "idor_self_ok")
        with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
            work, _ = _catalog(authz_session).add_from_isbn("9780441090003")
        authz_session.flush()

        place = authz_client.post(
            "/holds/",
            json={"work_id": work.id, "card_number": patron.library_card_number},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert place.status_code == 201
        hold_id = place.json()["id"]

        listed = authz_client.get(
            "/holds/",
            params={"card_number": patron.library_card_number},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert listed.status_code == 200
        assert any(h["id"] == hold_id for h in listed.json())

        cancel = authz_client.delete(
            f"/holds/{hold_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert cancel.status_code == 204


class TestInactiveUserTokens:
    """A valid JWT for an inactive user must not grant access anywhere."""

    def test_inactive_user_denied_on_authenticated_endpoint(self, authz_client, authz_session):
        user, _, token = _make_patron_user(authz_session, "inactive_self")
        user.is_active = False
        SqlUserRepository(authz_session).update(user)
        authz_session.commit()

        resp = authz_client.get("/me/loans", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_inactive_user_treated_as_anonymous_on_optional_endpoint(
        self, authz_client, authz_session
    ):
        """get_optional_user should return None for inactive users. When guest search is
        enabled (default) the request still succeeds anonymously."""
        user, _, token = _make_patron_user(authz_session, "inactive_opt")
        user.is_active = False
        SqlUserRepository(authz_session).update(user)
        authz_session.commit()

        resp = authz_client.get(
            "/works/search",
            params={"q": "dune"},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Guest search default = True, so anonymous access is allowed.
        assert resp.status_code == 200


class TestSelfDeactivation:
    def test_librarian_cannot_deactivate_themselves(self, authz_client, authz_session):
        user, token = _make_librarian(authz_session, "selfdeact_lib")
        resp = authz_client.post(
            f"/users/{user.username}/deactivate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422
        assert "own account" in resp.json()["detail"].lower()

    def test_librarian_can_deactivate_another_user(self, authz_client, authz_session):
        _, lib_token = _make_librarian(authz_session, "selfdeact_lib2")
        target, _, _ = _make_patron_user(authz_session, "selfdeact_target")
        resp = authz_client.post(
            f"/users/{target.username}/deactivate",
            headers={"Authorization": f"Bearer {lib_token}"},
        )
        assert resp.status_code == 200


class TestSecurityHeaders:
    def test_baseline_headers_present(self, authz_client):
        resp = authz_client.get("/ui/login")
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Referrer-Policy") == "no-referrer"
        assert "Content-Security-Policy" in resp.headers
