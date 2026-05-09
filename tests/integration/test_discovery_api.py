"""API tests for discovery endpoints (search filters, new arrivals, recently returned)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.db.session import get_session
from compendium.domain.enums import ItemStatus
from compendium.domain.models import (
    Base,
    Item,
    Loan,
    MediaType,
    Patron,
    Work,
)
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from tests.helpers import setup_sqlite_fts

_counter = {"n": 0}


def _next() -> int:
    _counter["n"] += 1
    return _counter["n"]


@pytest.fixture(scope="module")
def disc_engine():
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
def disc_session(disc_engine):
    factory = sessionmaker(bind=disc_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def disc_client(disc_engine):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=disc_engine, autoflush=False, expire_on_commit=False)
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


def _add_work(s: Session, *, title, media_code="book", year=None) -> Work:
    n = _next()
    mt = s.query(MediaType).filter_by(code=media_code).one()
    w = Work(
        title=title,
        media_type_id=mt.id,
        publication_year=year,
        search_text=title,
    )
    s.add(w)
    s.flush()
    s.commit()
    return w


def _add_item(s: Session, w: Work, *, status=ItemStatus.AVAILABLE.value) -> Item:
    n = _next()
    branch = SqlBranchRepository(s).get_default()
    it = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode=f"DABC{n:06d}",
        accession_number=f"DAACC{n:06d}",
        status=status,
    )
    s.add(it)
    s.flush()
    s.commit()
    return it


class TestSearchFilters:
    def test_filter_by_media(self, disc_client, disc_session):
        b = _add_work(disc_session, title="ApiBook one", media_code="book")
        d = _add_work(disc_session, title="ApiBook two", media_code="dvd")
        _add_item(disc_session, b)
        _add_item(disc_session, d)

        resp = disc_client.get("/works/search?q=ApiBook&field=title&media=dvd")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert d.id in ids
        assert b.id not in ids

    def test_filter_by_decade(self, disc_client, disc_session):
        old = _add_work(disc_session, title="ApiDecade alpha", year=1995)
        new = _add_work(disc_session, title="ApiDecade beta", year=2015)
        _add_item(disc_session, old)
        _add_item(disc_session, new)

        resp = disc_client.get(
            "/works/search?q=ApiDecade&field=title&decade=2010"
        )
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert new.id in ids
        assert old.id not in ids


class TestNewArrivalsApi:
    def test_returns_works(self, disc_client, disc_session):
        w = _add_work(disc_session, title="ApiNewArrival")
        _add_item(disc_session, w)
        # Force created_at recent
        disc_session.execute(
            Work.__table__.update().where(Work.id == w.id).values(
                created_at=datetime.now(tz=timezone.utc) - timedelta(days=1)
            )
        )
        disc_session.commit()
        resp = disc_client.get("/works/new-arrivals?days=30&limit=50")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert w.id in ids


class TestRecentlyReturnedApi:
    def test_returns_works_with_recent_returns(self, disc_client, disc_session):
        w = _add_work(disc_session, title="ApiReturnedTitle")
        item = _add_item(disc_session, w)
        n = _next()
        patron = Patron(library_card_number=f"DAP{n:05d}", full_name="P")
        disc_session.add(patron)
        disc_session.flush()
        branch = SqlBranchRepository(disc_session).get_default()
        now = datetime.now(tz=timezone.utc)
        loan = Loan(
            item_id=item.id,
            patron_id=patron.id,
            branch_id=branch.id,
            checked_out_at=now - timedelta(days=10),
            due_at=now,
            returned_at=now - timedelta(days=2),
        )
        disc_session.add(loan)
        disc_session.commit()

        resp = disc_client.get("/works/recently-returned?days=7&limit=50")
        assert resp.status_code == 200
        titles = [r["title"] for r in resp.json()]
        assert "ApiReturnedTitle" in titles
