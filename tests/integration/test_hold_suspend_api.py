"""API tests for hold suspend/resume endpoints."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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
from compendium.domain.enums import HoldStatus
from compendium.domain.models import AppUser, Base, Hold, Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.auth import AuthService, hash_password
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.holds import HoldService
from tests.helpers import setup_sqlite_fts

_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}
_SETTINGS = Settings(database_url="sqlite:///:memory:")

_n = {"i": 0}


def _next() -> int:
    _n["i"] += 1
    return _n["i"]


@pytest.fixture(scope="module")
def hs_engine():
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
def hs_session(hs_engine):
    factory = sessionmaker(bind=hs_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def hs_client(hs_engine):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=hs_engine, autoflush=False, expire_on_commit=False)
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
    u = AppUser(username=f"hslib{n}", password_hash=hash_password("pw"), role_id=role.id)
    SqlUserRepository(s).add(u)
    s.flush()
    s.commit()
    u.role = role
    return AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(u)


def _patron_token_and_patron(s: Session) -> tuple[str, Patron]:
    n = _next()
    role = SqlRoleRepository(s).get_by_name("Patron")
    u = AppUser(username=f"hsp{n}", password_hash=hash_password("pw"), role_id=role.id)
    SqlUserRepository(s).add(u)
    s.flush()
    patron = Patron(library_card_number=f"HSP{n:05d}", full_name="Alice", user_id=u.id)
    SqlPatronRepository(s).add(patron)
    s.flush()
    s.commit()
    u.role = role
    token = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(u)
    return token, patron


def _seed_waiting_hold(s: Session) -> tuple[Hold, Patron]:
    """Seed: work with one copy, check it out to one patron, place a hold for a
    second patron (which goes to WAITING). Returns (hold, second_patron)."""
    isbn = f"9780441{_next():06d}"
    with patch("compendium.services.metadata.lookup_isbn", return_value=_DUNE):
        catalog = CatalogService(
            work_repo=SqlWorkRepository(s),
            item_repo=SqlItemRepository(s),
            creator_repo=SqlCreatorRepository(s),
            branch_repo=SqlBranchRepository(s),
            media_type_repo=SqlMediaTypeRepository(s),
        )
        work, item = catalog.add_from_isbn(isbn)
    holder = Patron(library_card_number=f"HLD{_next():05d}", full_name="Holder")
    SqlPatronRepository(s).add(holder)
    s.flush()
    circ = CirculationService(
        item_repo=SqlItemRepository(s),
        loan_repo=__import__("compendium.repositories.sql.loan_repository", fromlist=["X"]).SqlLoanRepository(s),
        patron_repo=SqlPatronRepository(s),
        branch_repo=SqlBranchRepository(s),
        hold_repo=SqlHoldRepository(s),
        policy_repo=__import__("compendium.repositories.sql.loan_policy_repository", fromlist=["X"]).SqlLoanPolicyRepository(s),
    )
    circ.checkout(item.barcode, holder.library_card_number)
    waiter = Patron(library_card_number=f"WTR{_next():05d}", full_name="Waiter")
    SqlPatronRepository(s).add(waiter)
    s.flush()
    holds_svc = HoldService(
        hold_repo=SqlHoldRepository(s),
        patron_repo=SqlPatronRepository(s),
        work_repo=SqlWorkRepository(s),
        branch_repo=SqlBranchRepository(s),
        item_repo=SqlItemRepository(s),
    )
    hold = holds_svc.place(work.id, waiter.library_card_number)
    assert hold.status == HoldStatus.WAITING.value
    s.commit()
    return hold, waiter


class TestSuspendEndpoint:
    def test_librarian_can_suspend_any_hold(self, hs_client, hs_session):
        token = _librarian_token(hs_session)
        hold, _ = _seed_waiting_hold(hs_session)
        until = (date.today() + timedelta(days=14)).isoformat()
        resp = hs_client.post(
            f"/holds/{hold.id}/suspend",
            headers={"Authorization": f"Bearer {token}"},
            json={"until": until, "reason": "vacation"},
        )
        assert resp.status_code == 200
        assert resp.json()["suspended_until"] == until
        assert resp.json()["suspended_reason"] == "vacation"

    def test_patron_cannot_suspend_others_hold(self, hs_client, hs_session):
        hold, _waiter = _seed_waiting_hold(hs_session)
        # Third-party patron (different user)
        other_token, _ = _patron_token_and_patron(hs_session)
        until = (date.today() + timedelta(days=7)).isoformat()
        resp = hs_client.post(
            f"/holds/{hold.id}/suspend",
            headers={"Authorization": f"Bearer {other_token}"},
            json={"until": until},
        )
        assert resp.status_code == 403

    def test_suspend_validation_rejects_past_date(self, hs_client, hs_session):
        token = _librarian_token(hs_session)
        hold, _ = _seed_waiting_hold(hs_session)
        past = (date.today() - timedelta(days=1)).isoformat()
        resp = hs_client.post(
            f"/holds/{hold.id}/suspend",
            headers={"Authorization": f"Bearer {token}"},
            json={"until": past},
        )
        assert resp.status_code == 422


class TestResumeEndpoint:
    def test_librarian_can_resume(self, hs_client, hs_session):
        token = _librarian_token(hs_session)
        hold, _ = _seed_waiting_hold(hs_session)
        # Suspend first
        hold.suspended_until = date.today() + timedelta(days=7)
        hs_session.flush()
        hs_session.commit()
        resp = hs_client.post(
            f"/holds/{hold.id}/resume",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["suspended_until"] is None


class TestSelfServiceSuspend:
    def test_patron_can_suspend_own_hold(self, hs_client, hs_session):
        # Set up: hold belongs to a patron whose user we control
        patron_token, patron = _patron_token_and_patron(hs_session)
        # Seed a WAITING hold for that exact patron (not via _seed_waiting_hold)
        isbn = f"9780441{_next():06d}"
        with patch("compendium.services.metadata.lookup_isbn", return_value=_DUNE):
            catalog = CatalogService(
                work_repo=SqlWorkRepository(hs_session),
                item_repo=SqlItemRepository(hs_session),
                creator_repo=SqlCreatorRepository(hs_session),
                branch_repo=SqlBranchRepository(hs_session),
                media_type_repo=SqlMediaTypeRepository(hs_session),
            )
            work, item = catalog.add_from_isbn(isbn)
        # Another patron checks the copy out so our patron's hold goes WAITING
        holder = Patron(library_card_number=f"HO{_next():05d}", full_name="Holder")
        SqlPatronRepository(hs_session).add(holder)
        hs_session.flush()
        from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
        from compendium.repositories.sql.loan_repository import SqlLoanRepository

        circ = CirculationService(
            item_repo=SqlItemRepository(hs_session),
            loan_repo=SqlLoanRepository(hs_session),
            patron_repo=SqlPatronRepository(hs_session),
            branch_repo=SqlBranchRepository(hs_session),
            hold_repo=SqlHoldRepository(hs_session),
            policy_repo=SqlLoanPolicyRepository(hs_session),
        )
        circ.checkout(item.barcode, holder.library_card_number)
        holds_svc = HoldService(
            hold_repo=SqlHoldRepository(hs_session),
            patron_repo=SqlPatronRepository(hs_session),
            work_repo=SqlWorkRepository(hs_session),
            branch_repo=SqlBranchRepository(hs_session),
            item_repo=SqlItemRepository(hs_session),
        )
        hold = holds_svc.place(work.id, patron.library_card_number)
        assert hold.status == HoldStatus.WAITING.value
        hs_session.commit()

        until = (date.today() + timedelta(days=14)).isoformat()
        resp = hs_client.post(
            f"/me/holds/{hold.id}/suspend",
            headers={"Authorization": f"Bearer {patron_token}"},
            json={"until": until},
        )
        assert resp.status_code == 200
        assert resp.json()["suspended_until"] == until
