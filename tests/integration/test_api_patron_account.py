"""Integration tests for patron ↔ user account API endpoints.

Covers:
- POST /patrons with inline account block (patron.account.manage gating)
- POST /patrons/{card}/account
- POST /users with patron block (role escalation + patron link)
- POST/DELETE /users/{username}/patron
"""
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
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService, hash_password
from tests.helpers import setup_sqlite_fts

_TEST_SETTINGS = Settings(
    database_url="sqlite:///:memory:",
    jwt_secret_key="insecure-default-change-in-production",
)


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


def _token(s: Session, username: str, role_name: str) -> str:
    u = _make_user(s, username, role_name)
    return AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_TEST_SETTINGS,
    ).issue_token(u)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_patron(s: Session, card: str = "PAT-0001", name: str = "Test Patron") -> Patron:
    p = Patron(library_card_number=card, full_name=name)
    SqlPatronRepository(s).add(p)
    s.flush()
    return p


class TestPostPatronWithInlineAccount:
    def test_librarian_can_create_patron_with_account(self, client, db):
        tok = _token(db, "lib_acct1", "Librarian")
        resp = client.post(
            "/patrons",
            json={
                "full_name": "Inline User",
                "account": {"username": "inline_user1", "password": "Str0ng!Pass"},
            },
            headers=_auth(tok),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["full_name"] == "Inline User"
        patron = SqlPatronRepository(db).get_by_card_number(data["library_card_number"])
        assert patron is not None
        linked = SqlUserRepository(db).get_by_username("inline_user1")
        assert linked is not None
        assert patron.user_id == linked.id

    def test_duplicate_username_returns_409(self, client, db):
        tok = _token(db, "lib_acct2", "Librarian")
        _make_user(db, "taken_acct1", "Patron")
        db.commit()
        resp = client.post(
            "/patrons",
            json={
                "full_name": "Dupe User",
                "account": {"username": "taken_acct1", "password": "Str0ng!Pass"},
            },
            headers=_auth(tok),
        )
        assert resp.status_code == 409

    def test_missing_patron_account_manage_returns_403(self, client, db):
        # SystemAdmin has user.manage but not patron.manage / patron.account.manage
        tok = _token(db, "sysadm_acct1", "SystemAdmin")
        resp = client.post(
            "/patrons",
            json={
                "full_name": "No Perm",
                "account": {"username": "no_perm_u1", "password": "Str0ng!Pass"},
            },
            headers=_auth(tok),
        )
        assert resp.status_code == 403

    def test_patron_without_account_block_still_works(self, client, db):
        tok = _token(db, "lib_acct3", "Librarian")
        resp = client.post(
            "/patrons",
            json={"full_name": "No Account"},
            headers=_auth(tok),
        )
        assert resp.status_code == 201
        assert resp.json()["full_name"] == "No Account"


class TestPostPatronAccount:
    def test_creates_account_for_existing_patron(self, client, db):
        tok = _token(db, "lib_acct4", "Librarian")
        patron = _make_patron(db, "PAT-A001", "Card-Only Person")
        db.commit()
        resp = client.post(
            f"/patrons/{patron.library_card_number}/account",
            json={"username": "card_only_login1", "password": "Str0ng!Pass"},
            headers=_auth(tok),
        )
        assert resp.status_code == 200
        linked = SqlUserRepository(db).get_by_username("card_only_login1")
        assert linked is not None
        db.refresh(patron)
        assert patron.user_id == linked.id

    def test_returns_404_for_unknown_card(self, client, db):
        tok = _token(db, "lib_acct5", "Librarian")
        resp = client.post(
            "/patrons/NONEXISTENT/account",
            json={"username": "nobody1", "password": "Str0ng!Pass"},
            headers=_auth(tok),
        )
        assert resp.status_code == 404

    def test_returns_422_when_patron_already_linked(self, client, db):
        tok = _token(db, "lib_acct6", "Librarian")
        existing_user = _make_user(db, "already_linked1", "Patron")
        patron = _make_patron(db, "PAT-A002", "Already Linked")
        patron.user_id = existing_user.id
        db.flush()
        db.commit()
        resp = client.post(
            f"/patrons/{patron.library_card_number}/account",
            json={"username": "newlogin1", "password": "Str0ng!Pass"},
            headers=_auth(tok),
        )
        assert resp.status_code == 422

    def test_requires_patron_account_manage(self, client, db):
        tok = _token(db, "sysadm_acct2", "SystemAdmin")
        patron = _make_patron(db, "PAT-A003")
        db.commit()
        resp = client.post(
            f"/patrons/{patron.library_card_number}/account",
            json={"username": "blocked1", "password": "Str0ng!Pass"},
            headers=_auth(tok),
        )
        assert resp.status_code == 403


class TestPostUser:
    def test_admin_creates_staff_user(self, client, db):
        tok = _token(db, "admin_cr1", "Administrator")
        resp = client.post(
            "/users",
            json={"username": "new_lib1", "password": "Str0ng!Pass", "role_name": "Librarian"},
            headers=_auth(tok),
        )
        assert resp.status_code == 201
        assert resp.json()["username"] == "new_lib1"

    def test_admin_creates_patron_user_with_new_patron(self, client, db):
        tok = _token(db, "admin_cr2", "Administrator")
        resp = client.post(
            "/users",
            json={
                "username": "patron_inline1",
                "password": "Str0ng!Pass",
                "role_name": "Patron",
                "patron": {"create": {"full_name": "Inline Patron"}},
            },
            headers=_auth(tok),
        )
        assert resp.status_code == 201
        new_user = SqlUserRepository(db).get_by_username("patron_inline1")
        assert new_user is not None
        patron = SqlPatronRepository(db).get_by_user_id(new_user.id)
        assert patron is not None
        assert patron.full_name == "Inline Patron"

    def test_admin_creates_patron_user_linking_existing_patron(self, client, db):
        tok = _token(db, "admin_cr3", "Administrator")
        patron = _make_patron(db, "PAT-B001", "Card Holder")
        db.commit()
        resp = client.post(
            "/users",
            json={
                "username": "patron_link1",
                "password": "Str0ng!Pass",
                "role_name": "Patron",
                "patron": {"link_card": patron.library_card_number},
            },
            headers=_auth(tok),
        )
        assert resp.status_code == 201
        new_user = SqlUserRepository(db).get_by_username("patron_link1")
        db.refresh(patron)
        assert patron.user_id == new_user.id

    def test_patron_role_without_patron_block_returns_422(self, client, db):
        tok = _token(db, "admin_cr4", "Administrator")
        resp = client.post(
            "/users",
            json={"username": "no_patron_block1", "password": "Str0ng!Pass", "role_name": "Patron"},
            headers=_auth(tok),
        )
        assert resp.status_code == 422

    def test_role_escalation_blocked(self, client, db):
        # SystemAdmin cannot assign Administrator
        tok = _token(db, "sysadm_cr1", "SystemAdmin")
        resp = client.post(
            "/users",
            json={"username": "escalated1", "password": "Str0ng!Pass", "role_name": "Administrator"},
            headers=_auth(tok),
        )
        assert resp.status_code == 403

    def test_duplicate_username_returns_409(self, client, db):
        tok = _token(db, "admin_cr5", "Administrator")
        _make_user(db, "taken_user1", "Librarian")
        db.commit()
        resp = client.post(
            "/users",
            json={"username": "taken_user1", "password": "Str0ng!Pass", "role_name": "Librarian"},
            headers=_auth(tok),
        )
        assert resp.status_code == 409

    def test_requires_user_manage(self, client, db):
        tok = _token(db, "lib_cr1", "Librarian")
        resp = client.post(
            "/users",
            json={"username": "blocked2", "password": "Str0ng!Pass", "role_name": "Patron"},
            headers=_auth(tok),
        )
        assert resp.status_code == 403


class TestUserPatronLinkUnlink:
    def test_link_patron_to_user(self, client, db):
        tok = _token(db, "admin_lnk1", "Administrator")
        target = _make_user(db, "target_lnk1", "Patron")
        patron = _make_patron(db, "PAT-C001", "Link Target")
        db.commit()
        resp = client.post(
            f"/users/{target.username}/patron",
            json={"card_number": patron.library_card_number},
            headers=_auth(tok),
        )
        assert resp.status_code == 200
        db.refresh(patron)
        assert patron.user_id == target.id

    def test_unlink_patron_from_user(self, client, db):
        tok = _token(db, "admin_lnk2", "Administrator")
        target = _make_user(db, "target_lnk2", "Patron")
        patron = _make_patron(db, "PAT-C002", "Unlink Target")
        patron.user_id = target.id
        db.flush()
        db.commit()
        resp = client.delete(f"/users/{target.username}/patron", headers=_auth(tok))
        assert resp.status_code == 200
        db.refresh(patron)
        assert patron.user_id is None

    def test_link_returns_404_for_unknown_user(self, client, db):
        tok = _token(db, "admin_lnk3", "Administrator")
        resp = client.post(
            "/users/nobody_here/patron",
            json={"card_number": "PAT-FAKE"},
            headers=_auth(tok),
        )
        assert resp.status_code == 404

    def test_unlink_returns_404_when_no_patron_linked(self, client, db):
        tok = _token(db, "admin_lnk4", "Administrator")
        target = _make_user(db, "target_lnk4", "Patron")
        db.commit()
        resp = client.delete(f"/users/{target.username}/patron", headers=_auth(tok))
        assert resp.status_code == 404


class TestListPatrons:
    def test_list_search_and_status(self, client, db):
        tok = _token(db, "lib_list1", "Librarian")
        _make_patron(db, card="LIST-0001", name="Searchable Ada")
        p2 = _make_patron(db, card="LIST-0002", name="Dormant Bob")
        p2.is_active = False
        db.commit()

        # default status=active
        resp = client.get("/patrons", params={"q": "LIST-"}, headers=_auth(tok))
        assert resp.status_code == 200
        names = [p["full_name"] for p in resp.json()]
        assert "Searchable Ada" in names
        assert "Dormant Bob" not in names

        # status=all + name search
        resp = client.get(
            "/patrons", params={"status": "all", "q": "dormant b"}, headers=_auth(tok)
        )
        assert [p["full_name"] for p in resp.json()] == ["Dormant Bob"]

        # card-number search
        resp = client.get("/patrons", params={"q": "LIST-0001"}, headers=_auth(tok))
        assert [p["library_card_number"] for p in resp.json()] == ["LIST-0001"]

    def test_patron_role_forbidden(self, client, db):
        tok = _token(db, "pat_list1", "Patron")
        resp = client.get("/patrons", headers=_auth(tok))
        assert resp.status_code == 403

    def test_bad_status_rejected(self, client, db):
        tok = _token(db, "lib_list2", "Librarian")
        resp = client.get("/patrons", params={"status": "bogus"}, headers=_auth(tok))
        assert resp.status_code == 422
