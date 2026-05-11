"""Web UI tests for claims-returned workflows."""

from __future__ import annotations

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
from compendium.domain.enums import ItemStatus
from compendium.domain.models import AppUser, Base, Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.auth import hash_password
from compendium.services.catalog import CatalogService
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from tests.helpers import setup_sqlite_fts

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}
_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _derive_csrf_secret(_SECRET))}"


@pytest.fixture(scope="module")
def cw_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def cw_session(cw_engine):
    factory = sessionmaker(bind=cw_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def cw_client(cw_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: cw_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


def _login(client, session, username, role_name="Librarian") -> dict:
    role = SqlRoleRepository(session).get_by_name(role_name)
    user = AppUser(
        username=username, password_hash=hash_password("secret"), role_id=role.id
    )
    SqlUserRepository(session).add(user)
    session.flush()
    raw, signed = _csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": "secret", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    return dict(resp.cookies)


_n = {"i": 0}


def _next() -> int:
    _n["i"] += 1
    return _n["i"]


def _seed_loan(session, card_prefix="CW"):
    isbn = f"9780441{_next():06d}"
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        work, item = CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        ).add_from_isbn(isbn)
    n = _next()
    patron = Patron(library_card_number=f"{card_prefix}{n:05d}", full_name="Alice")
    SqlPatronRepository(session).add(patron)
    session.flush()
    # Create an active loan manually
    from datetime import datetime, timedelta, timezone

    from compendium.domain.models import Loan

    branch = SqlBranchRepository(session).get_default()
    loan = Loan(
        item_id=item.id,
        patron_id=patron.id,
        branch_id=branch.id,
        checked_out_at=datetime.now(timezone.utc) - timedelta(days=1),
        due_at=datetime.now(timezone.utc) + timedelta(days=13),
    )
    session.add(loan)
    item.status = ItemStatus.CHECKED_OUT.value
    session.flush()
    return work, item, patron, loan


class TestAdminClaimsPage:
    def test_empty_claims_list(self, cw_client, cw_session):
        cookies = _login(cw_client, cw_session, "cwlib1")
        resp = cw_client.get("/ui/admin/claims", cookies=cookies)
        assert resp.status_code == 200
        assert b"No outstanding claims" in resp.content

    def test_lists_active_claims(self, cw_client, cw_session):
        cookies = _login(cw_client, cw_session, "cwlib2")
        _, item, _, _ = _seed_loan(cw_session)
        item.status = ItemStatus.CLAIMS_RETURNED.value
        cw_session.flush()
        resp = cw_client.get("/ui/admin/claims", cookies=cookies)
        assert resp.status_code == 200
        assert item.barcode.encode() in resp.content

    def test_readonly_forbidden(self, cw_client, cw_session):
        cookies = _login(cw_client, cw_session, "cwro1", "ReadOnly")
        resp = cw_client.get("/ui/admin/claims", cookies=cookies)
        assert resp.status_code == 403


class TestVerifyReturnedRoute:
    def test_verify_redirects_with_success_message(self, cw_client, cw_session):
        cookies = _login(cw_client, cw_session, "cwlib3")
        _, item, _, _ = _seed_loan(cw_session)
        item.status = ItemStatus.CLAIMS_RETURNED.value
        cw_session.flush()
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = cw_client.post(
            f"/ui/items/{item.barcode}/verify-returned",
            data={"csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 303
        assert "Verified" in resp.headers["location"] or "verified" in resp.headers["location"]


class TestWriteOffClaimForm:
    def test_form_renders(self, cw_client, cw_session):
        cookies = _login(cw_client, cw_session, "cwlib4")
        _, item, _, _ = _seed_loan(cw_session)
        item.status = ItemStatus.CLAIMS_RETURNED.value
        cw_session.flush()
        resp = cw_client.get(
            f"/ui/items/{item.barcode}/write-off-claim", cookies=cookies
        )
        assert resp.status_code == 200
        assert b'name="note"' in resp.content


class TestPatronSelfService:
    def test_claim_returned_self_service(self, cw_client, cw_session):
        # Login as a patron whose user is linked to a specific patron with a loan
        _, item, patron, loan = _seed_loan(cw_session, card_prefix="SS")
        p_role = SqlRoleRepository(cw_session).get_by_name("Patron")
        n = _next()
        u = AppUser(
            username=f"pw{n}", password_hash=hash_password("secret"), role_id=p_role.id
        )
        SqlUserRepository(cw_session).add(u)
        cw_session.flush()
        patron.user_id = u.id
        cw_session.flush()
        cw_session.commit()  # so the login query sees it

        raw, signed = _csrf_pair()
        resp = cw_client.post(
            "/ui/login",
            data={"username": f"pw{n}", "password": "secret", "csrf_token": raw},
            cookies={CSRF_COOKIE: signed},
        )
        assert resp.status_code == 303
        cookies = dict(resp.cookies)
        raw2, signed2 = _csrf_pair()
        cookies[CSRF_COOKIE] = signed2
        resp = cw_client.post(
            f"/ui/me/loans/{loan.id}/claim-returned",
            data={"csrf_token": raw2},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert b"Claim submitted" in resp.content
        cw_session.expire_all()
        fresh = SqlItemRepository(cw_session).get(item.id)
        assert fresh.status == ItemStatus.CLAIMS_RETURNED.value
