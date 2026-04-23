"""API tests for claims-returned endpoints."""

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
from compendium.domain.models import AppUser, Base, Loan, Patron
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService, hash_password
from tests.helpers import setup_sqlite_fts

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}
_SETTINGS = Settings(database_url="sqlite:///:memory:")


@pytest.fixture(scope="module")
def cl_engine():
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
def cl_session(cl_engine):
    factory = sessionmaker(bind=cl_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def cl_client(cl_engine):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=cl_engine, autoflush=False, expire_on_commit=False)
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


def _issue(s: Session, username: str, role_name: str) -> tuple[AppUser, str]:
    n = _next()
    role = SqlRoleRepository(s).get_by_name(role_name)
    u = AppUser(
        username=f"{username}{n}", password_hash=hash_password("pw"), role_id=role.id
    )
    SqlUserRepository(s).add(u)
    s.flush()
    s.commit()
    u.role = role
    tok = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(u)
    return u, tok


def _seed_and_checkout(s: Session, token: str, client: TestClient, card_prefix: str):
    isbn = f"9780441{_next():06d}"
    from compendium.repositories.sql.branch_repository import SqlBranchRepository
    from compendium.repositories.sql.creator_repository import SqlCreatorRepository
    from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
    from compendium.repositories.sql.work_repository import SqlWorkRepository
    from compendium.services.catalog import CatalogService

    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        work, item = CatalogService(
            work_repo=SqlWorkRepository(s),
            item_repo=SqlItemRepository(s),
            creator_repo=SqlCreatorRepository(s),
            branch_repo=SqlBranchRepository(s),
            media_type_repo=SqlMediaTypeRepository(s),
        ).add_from_isbn(isbn)
    patron = Patron(library_card_number=f"{card_prefix}{_next():05d}", full_name="Alice")
    s.add(patron)
    s.flush()
    s.commit()
    resp = client.post(
        "/loans/checkout",
        headers={"Authorization": f"Bearer {token}"},
        json={"barcode": item.barcode, "card_number": patron.library_card_number},
    )
    assert resp.status_code == 201, resp.text
    loan_id = resp.json()["id"]
    return item, patron, loan_id


class TestClaimsListing:
    def test_empty_list(self, cl_client, cl_session):
        _, token = _issue(cl_session, "lib", "Librarian")
        resp = cl_client.get(
            "/loans/claims", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_requires_loan_checkin(self, cl_client, cl_session):
        _, token = _issue(cl_session, "ro", "ReadOnly")
        resp = cl_client.get(
            "/loans/claims", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403


class TestClaimEndpoint:
    def test_librarian_can_claim_any_loan(self, cl_client, cl_session):
        _, token = _issue(cl_session, "cliblib", "Librarian")
        item, _, loan_id = _seed_and_checkout(cl_session, token, cl_client, "CLA")
        resp = cl_client.post(
            f"/loans/{loan_id}/claim-returned",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        # API mutation happened via a separate session; expire our cached view.
        cl_session.expire_all()
        fresh = SqlItemRepository(cl_session).get_by_barcode(item.barcode)
        assert fresh.status == ItemStatus.CLAIMS_RETURNED.value

    def test_readonly_cannot_claim(self, cl_client, cl_session):
        _, lib_token = _issue(cl_session, "lib2", "Librarian")
        _, loan_id = (None, None)
        item, _, loan_id = _seed_and_checkout(cl_session, lib_token, cl_client, "CLB")
        _, ro_token = _issue(cl_session, "ro2", "ReadOnly")
        resp = cl_client.post(
            f"/loans/{loan_id}/claim-returned",
            headers={"Authorization": f"Bearer {ro_token}"},
        )
        assert resp.status_code == 403


class TestResolutions:
    def test_verify_returned_resolves_item(self, cl_client, cl_session):
        _, token = _issue(cl_session, "lib3", "Librarian")
        item, _, loan_id = _seed_and_checkout(cl_session, token, cl_client, "CLC")
        cl_client.post(
            f"/loans/{loan_id}/claim-returned",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = cl_client.post(
            f"/items/{item.barcode}/verify-returned",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == ItemStatus.AVAILABLE.value

    def test_write_off_requires_note(self, cl_client, cl_session):
        _, token = _issue(cl_session, "lib4", "Librarian")
        item, _, loan_id = _seed_and_checkout(cl_session, token, cl_client, "CLD")
        cl_client.post(
            f"/loans/{loan_id}/claim-returned",
            headers={"Authorization": f"Bearer {token}"},
        )
        # Empty note → 422
        resp = cl_client.post(
            f"/items/{item.barcode}/write-off-claim",
            headers={"Authorization": f"Bearer {token}"},
            json={"note": ""},
        )
        assert resp.status_code == 422

    def test_write_off_with_note_resolves(self, cl_client, cl_session):
        _, token = _issue(cl_session, "lib5", "Librarian")
        item, _, loan_id = _seed_and_checkout(cl_session, token, cl_client, "CLE")
        cl_client.post(
            f"/loans/{loan_id}/claim-returned",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = cl_client.post(
            f"/items/{item.barcode}/write-off-claim",
            headers={"Authorization": f"Bearer {token}"},
            json={"note": "Accepted patron's account"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == ItemStatus.AVAILABLE.value


class TestMeClaimReturned:
    def test_patron_can_claim_own_loan(self, cl_client, cl_session):
        # Patron needs a user account linked
        _, lib_token = _issue(cl_session, "lib6", "Librarian")
        item, patron, loan_id = _seed_and_checkout(cl_session, lib_token, cl_client, "CLF")
        # Create a user for this patron and link
        p_role = SqlRoleRepository(cl_session).get_by_name("Patron")
        n = _next()
        p_user = AppUser(
            username=f"pme{n}", password_hash=hash_password("pw"), role_id=p_role.id
        )
        SqlUserRepository(cl_session).add(p_user)
        cl_session.flush()
        patron.user_id = p_user.id
        cl_session.flush()
        cl_session.commit()
        p_user.role = p_role
        p_token = AuthService(
            user_repo=SqlUserRepository(cl_session),
            role_repo=SqlRoleRepository(cl_session),
            settings=_SETTINGS,
        ).issue_token(p_user)

        resp = cl_client.post(
            f"/me/loans/{loan_id}/claim-returned",
            headers={"Authorization": f"Bearer {p_token}"},
        )
        assert resp.status_code == 200

    def test_patron_cannot_claim_other_patrons_loan(self, cl_client, cl_session):
        _, lib_token = _issue(cl_session, "lib7", "Librarian")
        # Seed loan for one patron
        item, _, other_loan_id = _seed_and_checkout(cl_session, lib_token, cl_client, "CLG")
        # Create a DIFFERENT patron with a linked user
        p_role = SqlRoleRepository(cl_session).get_by_name("Patron")
        n = _next()
        other_user = AppUser(
            username=f"pother{n}", password_hash=hash_password("pw"), role_id=p_role.id
        )
        SqlUserRepository(cl_session).add(other_user)
        cl_session.flush()
        other_patron = Patron(
            library_card_number=f"OT{n:06d}",
            full_name="Bob",
            user_id=other_user.id,
        )
        cl_session.add(other_patron)
        cl_session.flush()
        cl_session.commit()
        other_user.role = p_role
        other_token = AuthService(
            user_repo=SqlUserRepository(cl_session),
            role_repo=SqlRoleRepository(cl_session),
            settings=_SETTINGS,
        ).issue_token(other_user)

        resp = cl_client.post(
            f"/me/loans/{other_loan_id}/claim-returned",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert resp.status_code == 403
