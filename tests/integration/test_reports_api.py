"""API tests for /reports/* endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    Loan,
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


@pytest.fixture(scope="module")
def rep_engine():
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
def rep_session(rep_engine):
    factory = sessionmaker(bind=rep_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def rep_client(rep_engine):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=rep_engine, autoflush=False, expire_on_commit=False)
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


_counter = {"n": 0}


def _next() -> int:
    _counter["n"] += 1
    return _counter["n"]


def _librarian_token(s: Session) -> str:
    n = _next()
    role = SqlRoleRepository(s).get_by_name("Librarian")
    user = AppUser(
        username=f"lib{n}",
        password_hash=hash_password("pw"),
        role_id=role.id,
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


def _readonly_token(s: Session) -> str:
    n = _next()
    role = SqlRoleRepository(s).get_by_name("ReadOnly")
    user = AppUser(
        username=f"ro{n}",
        password_hash=hash_password("pw"),
        role_id=role.id,
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


def _seed_loan(s: Session, *, title: str, checked_out_at: datetime, returned_at=None):
    n = _next()
    book = s.query(MediaType).filter_by(code="book").one()
    work = Work(title=title, media_type_id=book.id)
    s.add(work)
    s.flush()
    branch = SqlBranchRepository(s).get_default()
    item = Item(
        work_id=work.id,
        branch_id=branch.id,
        barcode=f"RBC{n:06d}",
        accession_number=f"RACC{n:06d}",
    )
    s.add(item)
    s.flush()
    patron = Patron(library_card_number=f"RC{n:05d}", full_name=f"P{n}")
    s.add(patron)
    s.flush()
    loan = Loan(
        item_id=item.id,
        patron_id=patron.id,
        branch_id=branch.id,
        checked_out_at=checked_out_at,
        due_at=checked_out_at + timedelta(days=14),
        returned_at=returned_at,
    )
    s.add(loan)
    s.flush()
    s.commit()
    return work, item, patron, loan


class TestAuth:
    def test_requires_auth(self, rep_client):
        resp = rep_client.get("/reports/checkouts")
        assert resp.status_code == 401

    def test_readonly_forbidden(self, rep_client, rep_session):
        token = _readonly_token(rep_session)
        resp = rep_client.get(
            "/reports/checkouts", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403


class TestCheckouts:
    def test_returns_month_series(self, rep_client, rep_session):
        token = _librarian_token(rep_session)
        resp = rep_client.get(
            "/reports/checkouts?months=3",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert {"month", "count"} <= set(data[0].keys())


class TestPopular:
    def test_returns_popular_list(self, rep_client, rep_session):
        token = _librarian_token(rep_session)
        _seed_loan(
            rep_session,
            title="PopularAPI",
            checked_out_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            returned_at=datetime(2026, 3, 10, tzinfo=timezone.utc),
        )
        resp = rep_client.get(
            "/reports/popular?from=2026-01-01&to=2026-05-01&limit=10",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        titles = [r["title"] for r in resp.json()]
        assert "PopularAPI" in titles

    def test_rejects_bad_date(self, rep_client, rep_session):
        token = _librarian_token(rep_session)
        resp = rep_client.get(
            "/reports/popular?from=not-a-date",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400


class TestOverdues:
    def test_lists_overdue(self, rep_client, rep_session):
        token = _librarian_token(rep_session)
        now = datetime.now(tz=timezone.utc)
        work, _, patron, _ = _seed_loan(
            rep_session,
            title="OverdueAPI",
            checked_out_at=now - timedelta(days=20),
        )
        # Force loan to be overdue by setting due_at in past
        loan = rep_session.query(Loan).filter_by(patron_id=patron.id).one()
        loan.due_at = now - timedelta(days=3)
        rep_session.commit()

        resp = rep_client.get(
            "/reports/overdues", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        titles = [r["title"] for r in resp.json()]
        assert "OverdueAPI" in titles


class TestDormant:
    def test_lists_never_loaned_item(self, rep_client, rep_session):
        token = _librarian_token(rep_session)
        n = _next()
        book = rep_session.query(MediaType).filter_by(code="book").one()
        work = Work(title="DormantAPI", media_type_id=book.id)
        rep_session.add(work)
        rep_session.flush()
        branch = SqlBranchRepository(rep_session).get_default()
        item = Item(
            work_id=work.id,
            branch_id=branch.id,
            barcode=f"DBC{n:06d}",
            accession_number=f"DACC{n:06d}",
        )
        rep_session.add(item)
        rep_session.commit()

        resp = rep_client.get(
            "/reports/dormant?not_since=2025-01-01&limit=200",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        barcodes = [r["barcode"] for r in resp.json()]
        assert item.barcode in barcodes
