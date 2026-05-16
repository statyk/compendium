"""API integration tests for /labels/*."""

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
from compendium.domain.models import (
    AppUser,
    Base,
    Item,
    MediaType,
    Patron,
    Work,
)
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService, hash_password
from tests.helpers import setup_sqlite_fts

_SETTINGS = Settings(database_url="sqlite:///:memory:")

_n = {"i": 0}


def _next() -> int:
    _n["i"] += 1
    return _n["i"]


@pytest.fixture(scope="module")
def lab_engine():
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
def lab_session(lab_engine):
    factory = sessionmaker(bind=lab_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def lab_client(lab_engine):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=lab_engine, autoflush=False, expire_on_commit=False)
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


def _librarian_token(s: Session) -> str:
    n = _next()
    role = SqlRoleRepository(s).get_by_name("Librarian")
    u = AppUser(username=f"lab{n}", password_hash=hash_password("pw"), role_id=role.id)
    SqlUserRepository(s).add(u)
    s.flush()
    s.commit()
    u.role = role
    return AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(u)


def _ro_token(s: Session) -> str:
    n = _next()
    role = SqlRoleRepository(s).get_by_name("ReadOnly")
    u = AppUser(username=f"lro{n}", password_hash=hash_password("pw"), role_id=role.id)
    SqlUserRepository(s).add(u)
    s.flush()
    s.commit()
    u.role = role
    return AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(u)


def _seed_item(s: Session, *, title="TestItem") -> Item:
    n = _next()
    mt = s.query(MediaType).filter_by(code="book").one()
    w = Work(title=title, media_type_id=mt.id)
    s.add(w)
    s.flush()
    branch = SqlBranchRepository(s).get_default()
    it = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode=f"LBC{n:06d}",
        accession_number=f"LACC{n:06d}",
        call_number="PS3551 .E76",
    )
    s.add(it)
    s.flush()
    s.commit()
    return it


def _seed_patron(s: Session) -> Patron:
    n = _next()
    p = Patron(library_card_number=f"LC{n:05d}", full_name=f"Patron {n}")
    s.add(p)
    s.flush()
    s.commit()
    return p


class TestAuth:
    def test_items_requires_labels_generate(self, lab_client, lab_session):
        token = _ro_token(lab_session)
        resp = lab_client.get(
            "/labels/items", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403

    def test_unauthenticated_rejected(self, lab_client):
        resp = lab_client.get("/labels/items")
        assert resp.status_code == 401


class TestItemLabels:
    def test_generates_pdf_with_no_filter(self, lab_client, lab_session):
        token = _librarian_token(lab_session)
        _seed_item(lab_session)
        resp = lab_client.get(
            "/labels/items", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")

    def test_returns_404_when_no_items_match(self, lab_client, lab_session):
        token = _librarian_token(lab_session)
        resp = lab_client.get(
            "/labels/items?barcodes=NOSUCH",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_rejects_unknown_template(self, lab_client, lab_session):
        token = _librarian_token(lab_session)
        _seed_item(lab_session)
        resp = lab_client.get(
            "/labels/items?template=not-a-template",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_barcodes_filter(self, lab_client, lab_session):
        token = _librarian_token(lab_session)
        i1 = _seed_item(lab_session, title="One")
        _seed_item(lab_session, title="Two")
        resp = lab_client.get(
            f"/labels/items?barcodes={i1.barcode}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF-")


class TestPatronCards:
    def test_generates_full_pdf(self, lab_client, lab_session):
        token = _librarian_token(lab_session)
        _seed_patron(lab_session)
        resp = lab_client.get(
            "/labels/patrons?template=avery-5871&format=full",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF-")

    def test_generates_sticker_pdf(self, lab_client, lab_session):
        token = _librarian_token(lab_session)
        _seed_patron(lab_session)
        resp = lab_client.get(
            "/labels/patrons?template=avery-5167&format=sticker",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF-")

    def test_rejects_invalid_format(self, lab_client, lab_session):
        token = _librarian_token(lab_session)
        resp = lab_client.get(
            "/labels/patrons?format=invalid",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    def test_returns_404_when_no_patrons_match(self, lab_client, lab_session):
        token = _librarian_token(lab_session)
        resp = lab_client.get(
            "/labels/patrons?cards=NOSUCH",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_full_on_small_template_rejected(self, lab_client, lab_session):
        token = _librarian_token(lab_session)
        _seed_patron(lab_session)
        resp = lab_client.get(
            "/labels/patrons?template=avery-5167&format=full",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422
        assert "too small" in resp.json()["detail"]


class TestNewFormats:
    """Tests for the new item label formats and templates added in barcode-label-revamp."""

    def test_api_spine_text_format(self, lab_client, lab_session):
        """GET /labels/items?format=spine-text should return a valid PDF."""
        token = _librarian_token(lab_session)
        _seed_item(lab_session)
        resp = lab_client.get(
            "/labels/items?format=spine-text",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")

    def test_api_spine_barcode_format(self, lab_client, lab_session):
        """GET /labels/items?format=spine-barcode should return a valid PDF."""
        token = _librarian_token(lab_session)
        _seed_item(lab_session)
        resp = lab_client.get(
            "/labels/items?format=spine-barcode",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")

    @pytest.mark.parametrize("template_key", ["avery-5167-spine", "avery-22805", "avery-22806"])
    def test_api_new_templates(self, lab_client, lab_session, template_key):
        """Each new template key should be accepted by the API and return a valid PDF."""
        token = _librarian_token(lab_session)
        _seed_item(lab_session)
        resp = lab_client.get(
            f"/labels/items?template={template_key}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")

    def test_api_invalid_format_rejected(self, lab_client, lab_session):
        """GET /labels/items?format=invalid-format should return 422."""
        token = _librarian_token(lab_session)
        resp = lab_client.get(
            "/labels/items?format=invalid-format",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422
