"""Integration tests for /me patron self-service endpoints."""

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
def me_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def me_session(me_engine) -> Session:
    factory = sessionmaker(bind=me_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def me_client(me_engine, me_session):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=me_engine, autoflush=False, expire_on_commit=False)
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


_patron_counter = 0


def _make_patron_user(s: Session, username: str) -> tuple[AppUser, Patron, str]:
    """Create a Patron-role user with a linked Patron record; return (user, patron, token)."""
    global _patron_counter
    _patron_counter += 1

    role = SqlRoleRepository(s).get_by_name("Patron")
    user = AppUser(
        username=username,
        password_hash=hash_password("password"),
        role_id=role.id,
    )
    SqlUserRepository(s).add(user)
    s.flush()
    user.role = role

    patron = Patron(
        library_card_number=f"SELF{_patron_counter:04d}",
        full_name=f"Patron {_patron_counter}",
        user_id=user.id,
    )
    SqlPatronRepository(s).add(patron)
    s.flush()

    token = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_TEST_SETTINGS,
    ).issue_token(user)
    return user, patron, token


def _catalog(s: Session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(s),
        item_repo=SqlItemRepository(s),
        creator_repo=SqlCreatorRepository(s),
        branch_repo=SqlBranchRepository(s),
        media_type_repo=SqlMediaTypeRepository(s),
    )


class TestMeLoans:
    def test_no_active_loans_returns_empty(self, me_client, me_session):
        _, _, token = _make_patron_user(me_session, "patron_loans1")
        resp = me_client.get("/me/loans", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_unauthenticated_returns_401(self, me_client, me_session):
        resp = me_client.get("/me/loans")
        assert resp.status_code == 401

    def test_user_without_patron_record_returns_403(self, me_client, me_session):
        role = SqlRoleRepository(me_session).get_by_name("ReadOnly")
        user = AppUser(
            username="nopat_user",
            password_hash=hash_password("password"),
            role_id=role.id,
        )
        SqlUserRepository(me_session).add(user)
        me_session.flush()
        user.role = role
        token = AuthService(
            user_repo=SqlUserRepository(me_session),
            role_repo=SqlRoleRepository(me_session),
            settings=_TEST_SETTINGS,
        ).issue_token(user)
        resp = me_client.get("/me/loans", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403


class TestMeHolds:
    def test_place_hold_via_me(self, me_client, me_session):
        _, _, token = _make_patron_user(me_session, "patron_holds1")
        with patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE):
            work, _ = _catalog(me_session).add_from_isbn("9780441013000")
        me_session.flush()

        resp = me_client.post(
            "/me/holds",
            json={"work_id": work.id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["work_id"] == work.id
        assert data["status"] == "waiting"

    def test_place_duplicate_hold_returns_422(self, me_client, me_session):
        _, _, token = _make_patron_user(me_session, "patron_holds2")
        with patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE):
            work, _ = _catalog(me_session).add_from_isbn("9780441013001")
        me_session.flush()

        me_client.post(
            "/me/holds",
            json={"work_id": work.id},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = me_client.post(
            "/me/holds",
            json={"work_id": work.id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    def test_list_and_cancel_hold(self, me_client, me_session):
        _, _, token = _make_patron_user(me_session, "patron_holds3")
        with patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE):
            work, _ = _catalog(me_session).add_from_isbn("9780441013002")
        me_session.flush()

        place = me_client.post(
            "/me/holds",
            json={"work_id": work.id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert place.status_code == 201
        hold_id = place.json()["id"]

        listed = me_client.get("/me/holds", headers={"Authorization": f"Bearer {token}"})
        assert any(h["id"] == hold_id for h in listed.json())

        cancel = me_client.delete(
            f"/me/holds/{hold_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert cancel.status_code == 204

        listed_after = me_client.get("/me/holds", headers={"Authorization": f"Bearer {token}"})
        assert not any(h["id"] == hold_id for h in listed_after.json())

    def test_cancel_other_patrons_hold_returns_422(self, me_client, me_session):
        _, patron_a, token_a = _make_patron_user(me_session, "patron_cancel_a")
        _, _, token_b = _make_patron_user(me_session, "patron_cancel_b")
        with patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE):
            work, _ = _catalog(me_session).add_from_isbn("9780441013003")
        me_session.flush()

        place = me_client.post(
            "/me/holds",
            json={"work_id": work.id},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        hold_id = place.json()["id"]

        resp = me_client.delete(
            f"/me/holds/{hold_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 422


class TestMeRenew:
    def test_patron_cannot_renew_via_loans_endpoint(self, me_client, me_session):
        """POST /loans/{id}/renew now requires loan.renew.any — patrons should get 403."""
        _, _, token = _make_patron_user(me_session, "patron_renew_check")
        resp = me_client.post(
            "/loans/999/renew",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_renew_own_loan_via_me(self, me_client, me_session):
        _, patron, token = _make_patron_user(me_session, "patron_renew1")
        with patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE):
            _, item = _catalog(me_session).add_from_isbn("9780441099001")
        me_session.flush()

        # Checkout via the librarian endpoint
        lib_role = SqlRoleRepository(me_session).get_by_name("Librarian")
        lib_user = AppUser(
            username="lib_for_renew",
            password_hash=hash_password("password"),
            role_id=lib_role.id,
        )
        SqlUserRepository(me_session).add(lib_user)
        me_session.flush()
        lib_user.role = lib_role
        lib_token = AuthService(
            user_repo=SqlUserRepository(me_session),
            role_repo=SqlRoleRepository(me_session),
            settings=_TEST_SETTINGS,
        ).issue_token(lib_user)

        checkout = me_client.post(
            "/loans/checkout",
            json={"barcode": item.barcode, "card_number": patron.library_card_number},
            headers={"Authorization": f"Bearer {lib_token}"},
        )
        assert checkout.status_code == 201
        loan_id = checkout.json()["id"]

        resp = me_client.post(
            f"/me/loans/{loan_id}/renew",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["renewal_count"] == 1

    def test_renew_other_patrons_loan_returns_422(self, me_client, me_session):
        _, patron_a, _ = _make_patron_user(me_session, "patron_renew_a")
        _, _, token_b = _make_patron_user(me_session, "patron_renew_b")
        with patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE):
            _, item = _catalog(me_session).add_from_isbn("9780441099002")
        me_session.flush()

        lib_role = SqlRoleRepository(me_session).get_by_name("Librarian")
        lib_user = AppUser(
            username="lib_for_renew2",
            password_hash=hash_password("password"),
            role_id=lib_role.id,
        )
        SqlUserRepository(me_session).add(lib_user)
        me_session.flush()
        lib_user.role = lib_role
        lib_token = AuthService(
            user_repo=SqlUserRepository(me_session),
            role_repo=SqlRoleRepository(me_session),
            settings=_TEST_SETTINGS,
        ).issue_token(lib_user)

        checkout = me_client.post(
            "/loans/checkout",
            json={"barcode": item.barcode, "card_number": patron_a.library_card_number},
            headers={"Authorization": f"Bearer {lib_token}"},
        )
        loan_id = checkout.json()["id"]

        resp = me_client.post(
            f"/me/loans/{loan_id}/renew",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 422
