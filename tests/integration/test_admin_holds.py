"""Tests for librarian hold visibility — repo, service, web, API."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.models import AppUser, Base, Hold, Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.auth import AuthService, hash_password
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.holds import HoldService
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from tests.helpers import setup_sqlite_fts

_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}
_FOUNDATION = {
    "title": "Foundation",
    "authors": [{"name": "Isaac Asimov"}],
    "publishers": [{"name": "Gnome"}],
    "publish_date": "1951",
    "cover": {},
    "identifiers": {},
}
_SETTINGS = Settings(database_url="sqlite:///:memory:")


_n = {"i": 0}


def _next() -> int:
    _n["i"] += 1
    return _n["i"]


@pytest.fixture(scope="module")
def ah_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def ah_session(ah_engine):
    factory = sessionmaker(bind=ah_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


def _catalog(session, meta):
    with patch("compendium.services.metadata.lookup_isbn", return_value=meta):
        cat = CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        )
        isbn = f"9780441{_next():06d}"
        return cat.add_from_isbn(isbn)


def _seed_work_with_waiting_hold(session, meta=_DUNE) -> tuple[object, object, Patron]:
    """Catalog a work, check out its only copy to an unrelated patron, then
    seed a second patron who places a hold (goes to WAITING)."""
    work, item = _catalog(session, meta)
    holder = Patron(library_card_number=f"HLD{_next():05d}", full_name="Holder")
    SqlPatronRepository(session).add(holder)
    session.flush()
    circ = CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )
    circ.checkout(item.barcode, holder.library_card_number)
    waiter = Patron(library_card_number=f"WTR{_next():05d}", full_name=f"Waiter {_next()}")
    SqlPatronRepository(session).add(waiter)
    session.flush()
    holds_svc = HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
    )
    hold = holds_svc.place(work.id, waiter.library_card_number)
    assert hold.status == HoldStatus.WAITING.value
    session.commit()
    return work, hold, waiter


# ──────────────────────────────────────────────────────────────────────────────
# Repository: list_active / count_active / queue_for_work / queue_position
# ──────────────────────────────────────────────────────────────────────────────


class TestHoldRepositoryListViews:
    def test_list_active_default_returns_waiting_and_available(self, ah_session):
        _, hold_a, _ = _seed_work_with_waiting_hold(ah_session)
        # Separate work — one copy available → immediate-promote to AVAILABLE
        work_b, _ = _catalog(ah_session, _FOUNDATION)
        patron_b = Patron(
            library_card_number=f"B{_next():04d}", full_name="B"
        )
        SqlPatronRepository(ah_session).add(patron_b)
        ah_session.flush()
        hs = HoldService(
            hold_repo=SqlHoldRepository(ah_session),
            patron_repo=SqlPatronRepository(ah_session),
            work_repo=SqlWorkRepository(ah_session),
            branch_repo=SqlBranchRepository(ah_session),
            item_repo=SqlItemRepository(ah_session),
        )
        hold_b = hs.place(work_b.id, patron_b.library_card_number)
        assert hold_b.status == HoldStatus.AVAILABLE.value
        ah_session.commit()

        rows = SqlHoldRepository(ah_session).list_active()
        ids = [h.id for h in rows]
        assert hold_a.id in ids
        assert hold_b.id in ids

    def test_list_active_status_filter(self, ah_session):
        _, hold, _ = _seed_work_with_waiting_hold(ah_session)
        repo = SqlHoldRepository(ah_session)
        waiting = repo.list_active(status=HoldStatus.WAITING.value)
        assert any(h.id == hold.id for h in waiting)
        avail = repo.list_active(status=HoldStatus.AVAILABLE.value)
        assert not any(h.id == hold.id for h in avail)

    def test_list_active_query_matches_patron_name(self, ah_session):
        _, hold, waiter = _seed_work_with_waiting_hold(ah_session)
        repo = SqlHoldRepository(ah_session)
        matches = repo.list_active(query=waiter.full_name)
        assert any(h.id == hold.id for h in matches)

    def test_list_active_query_matches_work_title(self, ah_session):
        _, hold, _ = _seed_work_with_waiting_hold(ah_session, meta=_DUNE)
        repo = SqlHoldRepository(ah_session)
        matches = repo.list_active(query="Dune")
        assert any(h.id == hold.id for h in matches)

    def test_count_active_matches_list(self, ah_session):
        _seed_work_with_waiting_hold(ah_session)
        _seed_work_with_waiting_hold(ah_session, meta=_FOUNDATION)
        repo = SqlHoldRepository(ah_session)
        assert repo.count_active() == len(repo.list_active(limit=500))

    def test_queue_for_work_ordered_by_placed_at(self, ah_session):
        work, hold_a, _ = _seed_work_with_waiting_hold(ah_session)
        # Add a second waiter (work already checked out, so goes WAITING too)
        waiter2 = Patron(
            library_card_number=f"W2_{_next():04d}", full_name="Second"
        )
        SqlPatronRepository(ah_session).add(waiter2)
        ah_session.flush()
        hs = HoldService(
            hold_repo=SqlHoldRepository(ah_session),
            patron_repo=SqlPatronRepository(ah_session),
            work_repo=SqlWorkRepository(ah_session),
            branch_repo=SqlBranchRepository(ah_session),
            item_repo=SqlItemRepository(ah_session),
        )
        hold_b = hs.place(work.id, waiter2.library_card_number)
        ah_session.commit()
        queue = SqlHoldRepository(ah_session).queue_for_work(work.id)
        assert [h.id for h in queue] == [hold_a.id, hold_b.id]

    def test_queue_position(self, ah_session):
        work, hold_a, _ = _seed_work_with_waiting_hold(ah_session)
        waiter2 = Patron(
            library_card_number=f"QP_{_next():04d}", full_name="Second"
        )
        SqlPatronRepository(ah_session).add(waiter2)
        ah_session.flush()
        hs = HoldService(
            hold_repo=SqlHoldRepository(ah_session),
            patron_repo=SqlPatronRepository(ah_session),
            work_repo=SqlWorkRepository(ah_session),
            branch_repo=SqlBranchRepository(ah_session),
            item_repo=SqlItemRepository(ah_session),
        )
        hold_b = hs.place(work.id, waiter2.library_card_number)
        ah_session.commit()
        repo = SqlHoldRepository(ah_session)
        assert repo.queue_position(hold_a.id) == 1
        assert repo.queue_position(hold_b.id) == 2

    def test_queue_position_none_for_cancelled(self, ah_session):
        _, hold, _ = _seed_work_with_waiting_hold(ah_session)
        hold.status = HoldStatus.CANCELLED.value
        ah_session.flush()
        ah_session.commit()
        assert SqlHoldRepository(ah_session).queue_position(hold.id) is None


# ──────────────────────────────────────────────────────────────────────────────
# Web UI: /ui/admin/holds + catalog detail queue block
# ──────────────────────────────────────────────────────────────────────────────


def _csrf_pair(settings: Settings) -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _derive_csrf_secret(settings.jwt_secret_key))}"


@pytest.fixture(scope="module")
def aw_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def aw_session(aw_engine):
    factory = sessionmaker(bind=aw_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def aw_client(aw_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: aw_session
    with patch(
        "compendium.db.engine.get_settings",
        return_value=Settings(database_url="sqlite:///:memory:"),
    ):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


def _login(client, session, role_name: str) -> dict:
    role = SqlRoleRepository(session).get_by_name(role_name)
    username = f"u{_next()}"
    user = AppUser(
        username=username, password_hash=hash_password("secret"), role_id=role.id
    )
    SqlUserRepository(session).add(user)
    session.flush()
    session.commit()
    settings = Settings(database_url="sqlite:///:memory:")
    raw, signed = _csrf_pair(settings)
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": "secret", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303, resp.text
    return dict(resp.cookies)


class TestAdminHoldsPage:
    def test_librarian_sees_holds(self, aw_client, aw_session):
        _seed_work_with_waiting_hold(aw_session)
        cookies = _login(aw_client, aw_session, "Librarian")
        resp = aw_client.get("/ui/admin/holds", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Holds" in body
        assert "matching hold" in body

    def test_patron_forbidden(self, aw_client, aw_session):
        cookies = _login(aw_client, aw_session, "Patron")
        resp = aw_client.get("/ui/admin/holds", cookies=cookies)
        assert resp.status_code in (302, 303, 403)

    def test_status_filter_narrows_results(self, aw_client, aw_session):
        _seed_work_with_waiting_hold(aw_session)
        cookies = _login(aw_client, aw_session, "Librarian")
        resp = aw_client.get(
            "/ui/admin/holds?status=available", cookies=cookies
        )
        assert resp.status_code == 200
        body = resp.content.decode()
        # The seeded hold was WAITING; status=available narrows to 0
        assert "0 matching hold" in body or "No holds match" in body


class TestWorkDetailQueueBlock:
    def test_queue_block_visible_to_librarian(self, aw_client, aw_session):
        work, _, _ = _seed_work_with_waiting_hold(aw_session)
        cookies = _login(aw_client, aw_session, "Librarian")
        resp = aw_client.get(f"/ui/catalog/{work.id}", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Hold queue" in body

    def test_queue_block_hidden_from_patron(self, aw_client, aw_session):
        work, _, _ = _seed_work_with_waiting_hold(aw_session)
        cookies = _login(aw_client, aw_session, "Patron")
        resp = aw_client.get(f"/ui/catalog/{work.id}", cookies=cookies)
        assert resp.status_code == 200
        assert "Hold queue" not in resp.content.decode()


# ──────────────────────────────────────────────────────────────────────────────
# API: GET /holds (system-wide) + GET /holds/queue/{id}
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def api_engine():
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
def api_session(api_engine):
    factory = sessionmaker(bind=api_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def api_client(api_engine):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=api_engine, autoflush=False, expire_on_commit=False)
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


def _issue_token(session, role_name: str) -> str:
    role = SqlRoleRepository(session).get_by_name(role_name)
    user = AppUser(
        username=f"api{_next()}", password_hash=hash_password("pw"), role_id=role.id
    )
    SqlUserRepository(session).add(user)
    session.flush()
    session.commit()
    user.role = role
    return AuthService(
        user_repo=SqlUserRepository(session),
        role_repo=SqlRoleRepository(session),
        settings=_SETTINGS,
    ).issue_token(user)


class TestHoldsApiSystemWide:
    def test_librarian_can_list_all(self, api_client, api_session):
        _seed_work_with_waiting_hold(api_session)
        token = _issue_token(api_session, "Librarian")
        resp = api_client.get(
            "/holds/", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_patron_forbidden_without_card(self, api_client, api_session):
        token = _issue_token(api_session, "Patron")
        resp = api_client.get(
            "/holds/", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403

    def test_work_queue_endpoint(self, api_client, api_session):
        work, hold, _ = _seed_work_with_waiting_hold(api_session)
        token = _issue_token(api_session, "Librarian")
        resp = api_client.get(
            f"/holds/queue/{work.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        ids = [h["id"] for h in resp.json()]
        assert hold.id in ids


# ──────────────────────────────────────────────────────────────────────────────
# /me/holds queue_position surface
# ──────────────────────────────────────────────────────────────────────────────


class TestMeHoldsQueuePosition:
    def test_me_holds_shows_position(self, aw_client, aw_session):
        # Place hold as patron user
        role = SqlRoleRepository(aw_session).get_by_name("Patron")
        user = AppUser(
            username=f"p{_next()}",
            password_hash=hash_password("secret"),
            role_id=role.id,
        )
        SqlUserRepository(aw_session).add(user)
        aw_session.flush()
        patron = Patron(
            library_card_number=f"MP{_next():04d}",
            full_name="Me Patron",
            user_id=user.id,
        )
        SqlPatronRepository(aw_session).add(patron)
        aw_session.flush()
        # Seed a work checked out to someone else
        work, item = _catalog(aw_session, _DUNE)
        holder = Patron(library_card_number=f"H{_next():04d}", full_name="H")
        SqlPatronRepository(aw_session).add(holder)
        aw_session.flush()
        circ = CirculationService(
            item_repo=SqlItemRepository(aw_session),
            loan_repo=SqlLoanRepository(aw_session),
            patron_repo=SqlPatronRepository(aw_session),
            branch_repo=SqlBranchRepository(aw_session),
            hold_repo=SqlHoldRepository(aw_session),
            policy_repo=SqlLoanPolicyRepository(aw_session),
        )
        circ.checkout(item.barcode, holder.library_card_number)
        hs = HoldService(
            hold_repo=SqlHoldRepository(aw_session),
            patron_repo=SqlPatronRepository(aw_session),
            work_repo=SqlWorkRepository(aw_session),
            branch_repo=SqlBranchRepository(aw_session),
            item_repo=SqlItemRepository(aw_session),
        )
        hs.place(work.id, patron.library_card_number)
        aw_session.commit()

        settings = Settings(database_url="sqlite:///:memory:")
        raw, signed = _csrf_pair(settings)
        resp = aw_client.post(
            "/ui/login",
            data={"username": user.username, "password": "secret", "csrf_token": raw},
            cookies={CSRF_COOKIE: signed},
        )
        assert resp.status_code == 303
        cookies = dict(resp.cookies)
        resp = aw_client.get("/ui/me/holds", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        # Either "#1" as the position marker
        assert "#1" in body
