"""Web UI tests for /ui/catalog facets + landing-page lists."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)

_counter = {"n": 0}


def _next() -> int:
    _counter["n"] += 1
    return _counter["n"]


@pytest.fixture(scope="module")
def dw_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def dw_session(dw_engine):
    factory = sessionmaker(bind=dw_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def dw_client(dw_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: dw_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


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
    return w


def _add_item(s: Session, w: Work, *, status=ItemStatus.AVAILABLE.value) -> Item:
    n = _next()
    branch = SqlBranchRepository(s).get_default()
    it = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode=f"DWBC{n:06d}",
        accession_number=f"DWACC{n:06d}",
        status=status,
    )
    s.add(it)
    s.flush()
    return it


class TestLandingPage:
    def test_landing_renders_for_guest(self, dw_client, dw_session):
        # Guest search enabled by default → landing renders.
        resp = dw_client.get("/ui/catalog")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Catalog" in body

    def test_landing_shows_new_arrivals_section(self, dw_client, dw_session):
        w = _add_work(dw_session, title="LandingNewArrival")
        dw_session.execute(
            Work.__table__.update().where(Work.id == w.id).values(
                created_at=datetime.now(tz=timezone.utc) - timedelta(days=1)
            )
        )
        dw_session.flush()
        resp = dw_client.get("/ui/catalog")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "New arrivals" in body
        assert "LandingNewArrival" in body


class TestFacetsRender:
    def test_search_renders_facet_sidebar(self, dw_client, dw_session):
        _add_work(dw_session, title="FacetUI alpha", media_code="book", year=2010)
        _add_work(dw_session, title="FacetUI beta", media_code="dvd", year=2015)
        dw_session.flush()
        resp = dw_client.get("/ui/catalog?q=FacetUI&field=title")
        assert resp.status_code == 200
        body = resp.content.decode()
        # Both media types appear in the sidebar
        assert "Book" in body and "DVD" in body
        # Decade buckets shown
        assert "2010s" in body or "2010" in body

    def test_media_filter_narrows_results(self, dw_client, dw_session):
        _add_work(dw_session, title="MNarrow alpha", media_code="book")
        _add_work(dw_session, title="MNarrow beta", media_code="dvd")
        dw_session.flush()
        resp = dw_client.get("/ui/catalog?q=MNarrow&field=title&media=dvd")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "MNarrow beta" in body
        # Note: "MNarrow alpha" appears in the facet name list ("alpha"), so
        # check that the work entry isn't shown — search results section.
        # Easier: count occurrences.
        assert body.count("MNarrow alpha") == 0
