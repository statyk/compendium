"""API tests for /patron-categories CRUD + extended /patrons + /policies."""

from __future__ import annotations

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
from tests.helpers import setup_sqlite_fts

_SETTINGS = Settings(database_url="sqlite:///:memory:")


@pytest.fixture(scope="module")
def pc_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    s.close()
    return eng


@pytest.fixture
def pc_session(pc_engine):
    factory = sessionmaker(bind=pc_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def pc_client(pc_engine):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=pc_engine, autoflush=False, expire_on_commit=False)
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


_n = {"i": 0}


def _next() -> int:
    _n["i"] += 1
    return _n["i"]


def _librarian_token(s: Session) -> str:
    n = _next()
    role = SqlRoleRepository(s).get_by_name("Librarian")
    user = AppUser(
        username=f"pclib{n}", password_hash=hash_password("pw"), role_id=role.id
    )
    SqlUserRepository(s).add(user)
    s.flush()
    s.commit()
    user.role = role
    return AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(user)


def _ro_token(s: Session) -> str:
    n = _next()
    role = SqlRoleRepository(s).get_by_name("ReadOnly")
    user = AppUser(
        username=f"pcro{n}", password_hash=hash_password("pw"), role_id=role.id
    )
    SqlUserRepository(s).add(user)
    s.flush()
    s.commit()
    user.role = role
    return AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(user)


class TestCategoryEndpoints:
    def test_list_returns_seeded_categories(self, pc_client, pc_session):
        token = _librarian_token(pc_session)
        resp = pc_client.get(
            "/patron-categories/", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        codes = {c["code"] for c in resp.json()}
        assert {"adult", "child", "staff", "teacher"} <= codes

    def test_list_requires_patron_manage(self, pc_client, pc_session):
        token = _ro_token(pc_session)
        resp = pc_client.get(
            "/patron-categories/", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403

    def test_create_and_delete(self, pc_client, pc_session):
        token = _librarian_token(pc_session)
        resp = pc_client.post(
            "/patron-categories/",
            json={"code": "apicat1", "display_name": "API Cat 1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        cid = resp.json()["id"]

        resp = pc_client.delete(
            f"/patron-categories/{cid}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 204

    def test_create_duplicate_rejected(self, pc_client, pc_session):
        token = _librarian_token(pc_session)
        resp = pc_client.post(
            "/patron-categories/",
            json={"code": "adult", "display_name": "Already Exists"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422


class TestPolicyEndpoints:
    def test_delete_then_404(self, pc_client, pc_session):
        token = _librarian_token(pc_session)
        resp = pc_client.post(
            "/policies/",
            json={"name": "API Deletable", "loan_period_days": 14},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        pid = resp.json()["id"]

        resp = pc_client.delete(
            f"/policies/{pid}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 204

        resp = pc_client.delete(
            f"/policies/{pid}", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 404

    def test_delete_default_returns_422(self, pc_client, pc_session):
        from compendium.repositories.sql.loan_policy_repository import (
            SqlLoanPolicyRepository,
        )

        token = _librarian_token(pc_session)
        default_policy = SqlLoanPolicyRepository(pc_session).get_default()
        assert default_policy is not None

        resp = pc_client.delete(
            f"/policies/{default_policy.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422


class TestPatronCreateWithCategory:
    def test_creates_patron_with_category_code(self, pc_client, pc_session):
        token = _librarian_token(pc_session)
        resp = pc_client.post(
            "/patrons",
            json={
                "full_name": "Cat Patron",
                "category_code": "child",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["category_id"] is not None

    def test_unknown_category_code_returns_422(self, pc_client, pc_session):
        token = _librarian_token(pc_session)
        resp = pc_client.post(
            "/patrons",
            json={"full_name": "P", "category_code": "no-such"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422


class TestPatronPatch:
    def test_patch_sets_expiry(self, pc_client, pc_session):
        token = _librarian_token(pc_session)
        resp = pc_client.post(
            "/patrons",
            json={"full_name": "PatchMe"},
            headers={"Authorization": f"Bearer {token}"},
        )
        card = resp.json()["library_card_number"]

        resp = pc_client.patch(
            f"/patrons/{card}",
            json={"expires_at": "2027-01-01"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["expires_at"] == "2027-01-01"

    def test_patch_clears_category(self, pc_client, pc_session):
        token = _librarian_token(pc_session)
        resp = pc_client.post(
            "/patrons",
            json={"full_name": "ClearCat", "category_code": "child"},
            headers={"Authorization": f"Bearer {token}"},
        )
        card = resp.json()["library_card_number"]

        resp = pc_client.patch(
            f"/patrons/{card}",
            json={"category_code": None},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["category_id"] is None
