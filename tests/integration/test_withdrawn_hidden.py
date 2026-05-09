"""Works whose copies are all WITHDRAWN must be hidden from catalog by default.

Staff with item.edit may opt-in via include_withdrawn_only=True.
"""

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
from compendium.domain.enums import ItemStatus
from compendium.domain.models import Base, Item, Loan, MediaType, Patron, Work
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.discovery import DiscoveryService
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)

_counter = {"n": 0}


def _next() -> int:
    _counter["n"] += 1
    return _counter["n"]


# ---------------------------------------------------------------------------
# Unit-level fixtures (reuse conftest `session`)
# ---------------------------------------------------------------------------


def _add_work(session: Session, *, title: str, media_code: str = "book") -> Work:
    n = _next()
    mt = session.query(MediaType).filter_by(code=media_code).one()
    w = Work(title=title, media_type_id=mt.id, search_text=title)
    session.add(w)
    session.flush()
    return w


def _add_item(session: Session, work: Work, *, status: str = ItemStatus.AVAILABLE.value) -> Item:
    n = _next()
    branch = SqlBranchRepository(session).get_default()
    it = Item(
        work_id=work.id,
        branch_id=branch.id,
        barcode=f"WH{n:06d}",
        accession_number=f"WHACC{n:06d}",
        status=status,
        is_loanable=True,
    )
    session.add(it)
    session.flush()
    return it


def _svc(session: Session) -> DiscoveryService:
    return DiscoveryService(work_repo=SqlWorkRepository(session))


# ---------------------------------------------------------------------------
# Repository / service tests (use the shared conftest `session`)
# ---------------------------------------------------------------------------


class TestHideWithdrawnOnlyDefault:
    def test_search_hides_all_withdrawn_work(self, session):
        visible = _add_work(session, title="WH SearchVisible")
        hidden = _add_work(session, title="WH SearchHidden")
        _add_item(session, visible, status=ItemStatus.AVAILABLE.value)
        _add_item(session, hidden, status=ItemStatus.WITHDRAWN.value)

        page = _svc(session).search("WH Search", field="title")
        ids = {w.id for w in page.works}
        assert visible.id in ids
        assert hidden.id not in ids

    def test_search_shows_work_with_mixed_items(self, session):
        w = _add_work(session, title="WH Mixed")
        _add_item(session, w, status=ItemStatus.WITHDRAWN.value)
        _add_item(session, w, status=ItemStatus.AVAILABLE.value)

        page = _svc(session).search("WH Mixed", field="title")
        assert w.id in {x.id for x in page.works}

    def test_include_withdrawn_only_shows_hidden(self, session):
        visible = _add_work(session, title="WH IncW Visible")
        hidden = _add_work(session, title="WH IncW Hidden")
        _add_item(session, visible, status=ItemStatus.AVAILABLE.value)
        _add_item(session, hidden, status=ItemStatus.WITHDRAWN.value)

        page = _svc(session).search("WH IncW", field="title", include_withdrawn_only=True)
        ids = {w.id for w in page.works}
        assert visible.id in ids
        assert hidden.id in ids

    def test_work_with_no_items_is_hidden(self, session):
        w = _add_work(session, title="WH NoItems")
        page = _svc(session).search("WH NoItems", field="title")
        assert w.id not in {x.id for x in page.works}

    def test_new_arrivals_excludes_all_withdrawn(self, session):
        visible = _add_work(session, title="WH NArrival Vis")
        hidden = _add_work(session, title="WH NArrival Hid")
        _add_item(session, visible, status=ItemStatus.AVAILABLE.value)
        _add_item(session, hidden, status=ItemStatus.WITHDRAWN.value)
        # Both are "recent"
        for w in [visible, hidden]:
            session.execute(
                Work.__table__.update().where(Work.id == w.id).values(
                    created_at=datetime.now(tz=timezone.utc) - timedelta(days=1)
                )
            )
        session.flush()

        works = _svc(session).new_arrivals(days=60)
        ids = {w.id for w in works}
        assert visible.id in ids
        assert hidden.id not in ids

    def test_new_arrivals_include_withdrawn_shows_both(self, session):
        visible = _add_work(session, title="WH NAInc Vis")
        hidden = _add_work(session, title="WH NAInc Hid")
        _add_item(session, visible, status=ItemStatus.AVAILABLE.value)
        _add_item(session, hidden, status=ItemStatus.WITHDRAWN.value)
        for w in [visible, hidden]:
            session.execute(
                Work.__table__.update().where(Work.id == w.id).values(
                    created_at=datetime.now(tz=timezone.utc) - timedelta(days=1)
                )
            )
        session.flush()

        works = _svc(session).new_arrivals(days=60, include_withdrawn_only=True)
        ids = {w.id for w in works}
        assert visible.id in ids
        assert hidden.id in ids

    def test_lost_item_work_still_visible(self, session):
        w = _add_work(session, title="WH LostVisible")
        _add_item(session, w, status=ItemStatus.LOST.value)

        page = _svc(session).search("WH LostVisible", field="title")
        assert w.id in {x.id for x in page.works}


# ---------------------------------------------------------------------------
# Web UI integration tests (module-scoped engine to avoid StaticPool issues)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wh_engine():
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
def wh_session(wh_engine):
    factory = sessionmaker(bind=wh_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def wh_client(wh_engine):
    import compendium.db.engine as _eng_mod
    app = create_app()

    def _override():
        factory = sessionmaker(bind=wh_engine, autoflush=False, expire_on_commit=False)
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
    with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
        yield c


def _wh_add_work(s: Session, *, title: str) -> Work:
    n = _next()
    mt = s.query(MediaType).filter_by(code="book").one()
    w = Work(title=title, media_type_id=mt.id, search_text=title)
    s.add(w)
    s.flush()
    s.commit()
    return w


def _wh_add_item(s: Session, w: Work, *, status: str = ItemStatus.AVAILABLE.value) -> Item:
    n = _next()
    branch = SqlBranchRepository(s).get_default()
    it = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode=f"WHC{n:06d}",
        accession_number=f"WHCACC{n:06d}",
        status=status,
        is_loanable=True,
    )
    s.add(it)
    s.flush()
    s.commit()
    return it


class TestApiSearchWithdrawn:
    def test_api_search_hides_all_withdrawn_by_default(self, wh_client, wh_session):
        visible = _wh_add_work(wh_session, title="ApiWH Visible")
        hidden = _wh_add_work(wh_session, title="ApiWH Hidden")
        _wh_add_item(wh_session, visible)
        _wh_add_item(wh_session, hidden, status=ItemStatus.WITHDRAWN.value)

        resp = wh_client.get("/works/search?q=ApiWH&field=title")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert visible.id in ids
        assert hidden.id not in ids

    def test_api_search_include_withdrawn_ignored_without_auth(self, wh_client, wh_session):
        hidden = _wh_add_work(wh_session, title="ApiWH Anon Hidden")
        _wh_add_item(wh_session, hidden, status=ItemStatus.WITHDRAWN.value)

        resp = wh_client.get("/works/search?q=ApiWH+Anon+Hidden&field=title&include_withdrawn=true")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert hidden.id not in ids
