"""Integration tests for GET /audit REST endpoint."""

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
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.auth import AuthService, hash_password
from compendium.services.catalog import CatalogService

_TEST_SETTINGS = Settings(
    database_url="sqlite:///:memory:",
    jwt_secret_key="insecure-default-change-in-production",
)

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}


@pytest.fixture(scope="module")
def audit_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def audit_session(audit_engine) -> Session:
    factory = sessionmaker(bind=audit_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def audit_client(audit_engine, audit_session):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=audit_engine, autoflush=False, expire_on_commit=False)
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
    user = AppUser(username=username, email=None, password_hash=hash_password("password"), role_id=role.id)
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


class TestAuditEndpoint:
    def test_unauthenticated_returns_401(self, audit_client, audit_session):
        resp = audit_client.get("/audit/")
        assert resp.status_code == 401

    def test_patron_role_returns_403(self, audit_client, audit_session):
        token = _token(audit_session, "patron_a1", "Patron")
        resp = audit_client.get("/audit/", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_librarian_gets_audit_list(self, audit_client, audit_session):
        librarian = _make_user(audit_session, "lib_audit1", "Librarian")
        token = AuthService(
            user_repo=SqlUserRepository(audit_session),
            role_repo=SqlRoleRepository(audit_session),
            settings=_TEST_SETTINGS,
        ).issue_token(librarian)
        with patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE):
            from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
            from compendium.services.audit import AuditService

            CatalogService(
                work_repo=SqlWorkRepository(audit_session),
                item_repo=SqlItemRepository(audit_session),
                creator_repo=SqlCreatorRepository(audit_session),
                branch_repo=SqlBranchRepository(audit_session),
                media_type_repo=SqlMediaTypeRepository(audit_session),
                audit_svc=AuditService(SqlAuditLogRepository(audit_session)),
                actor=librarian,
                source="test",
            ).add_from_isbn("9780441013593")
            audit_session.flush()
        resp = audit_client.get("/audit/", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        entry = data[0]
        assert "entity_type" in entry
        assert "action" in entry
        assert "occurred_at" in entry

    def test_entity_type_filter(self, audit_client, audit_session):
        token = _token(audit_session, "lib_audit2", "Librarian")
        resp = audit_client.get(
            "/audit/?entity_type=item", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        for entry in resp.json():
            assert entry["entity_type"] == "item"

    def test_user_id_filter_returns_subset(self, audit_client, audit_session):
        token = _token(audit_session, "lib_audit3", "Librarian")
        resp = audit_client.get(
            "/audit/?user_id=999999", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.json() == []
