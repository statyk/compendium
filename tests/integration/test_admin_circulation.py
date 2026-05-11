"""Tests for slice #12b — admin loans, admin fines, patron history, item history."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from compendium.domain.enums import FineKind, FineStatus
from compendium.domain.models import AppUser, Base, Fine, Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
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


# ──────────────────────────────────────────────────────────────────────────────
# Repository layer
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def repo_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def repo_session(repo_engine):
    factory = sessionmaker(bind=repo_engine, autoflush=False, expire_on_commit=False)
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


def _circ(session) -> CirculationService:
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )


def _patron(session, name: str) -> Patron:
    p = Patron(library_card_number=f"P{_next():06d}", full_name=name)
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


class TestLoanRepositoryListActive:
    def test_list_active_default_returns_only_open(self, repo_session):
        work, item = _catalog(repo_session, _DUNE)
        p = _patron(repo_session, "Alice")
        loan = _circ(repo_session).checkout(item.barcode, p.library_card_number)
        repo_session.commit()
        active = SqlLoanRepository(repo_session).list_active()
        assert any(l.id == loan.id for l in active)
        # Close the loan
        _circ(repo_session).checkin_by_id(loan.id)
        repo_session.commit()
        active = SqlLoanRepository(repo_session).list_active()
        assert not any(l.id == loan.id for l in active)

    def test_list_active_overdue_filter(self, repo_session):
        work, item = _catalog(repo_session, _DUNE)
        p = _patron(repo_session, "Bob")
        loan = _circ(repo_session).checkout(item.barcode, p.library_card_number)
        # Force overdue: set due_at in the past
        loan.due_at = datetime.now(tz=timezone.utc) - timedelta(days=1)
        repo_session.flush()
        repo_session.commit()
        repo = SqlLoanRepository(repo_session)
        assert any(l.id == loan.id for l in repo.list_active(due="overdue"))
        assert not any(l.id == loan.id for l in repo.list_active(due="on_time"))

    def test_list_active_query_matches_barcode(self, repo_session):
        work, item = _catalog(repo_session, _DUNE)
        p = _patron(repo_session, "Carol")
        _circ(repo_session).checkout(item.barcode, p.library_card_number)
        repo_session.commit()
        matches = SqlLoanRepository(repo_session).list_active(query=item.barcode)
        assert any(l.item.barcode == item.barcode for l in matches)


class TestLoanRepositoryPatronHistory:
    def test_list_for_patron_status_toggle(self, repo_session):
        p = _patron(repo_session, "Dan")
        w1, i1 = _catalog(repo_session, _DUNE)
        w2, i2 = _catalog(repo_session, _FOUNDATION)
        l1 = _circ(repo_session).checkout(i1.barcode, p.library_card_number)
        l2 = _circ(repo_session).checkout(i2.barcode, p.library_card_number)
        _circ(repo_session).checkin_by_id(l1.id)
        repo_session.commit()
        repo = SqlLoanRepository(repo_session)
        assert [l.id for l in repo.list_for_patron(p.id, status="active")] == [l2.id]
        assert [l.id for l in repo.list_for_patron(p.id, status="returned")] == [l1.id]
        assert {l.id for l in repo.list_for_patron(p.id, status="all")} == {l1.id, l2.id}

    def test_count_for_patron(self, repo_session):
        p = _patron(repo_session, "Eve")
        w, i = _catalog(repo_session, _DUNE)
        loan = _circ(repo_session).checkout(i.barcode, p.library_card_number)
        repo_session.commit()
        repo = SqlLoanRepository(repo_session)
        assert repo.count_for_patron(p.id, status="active") == 1
        _circ(repo_session).checkin_by_id(loan.id)
        repo_session.commit()
        assert repo.count_for_patron(p.id, status="active") == 0
        assert repo.count_for_patron(p.id, status="returned") == 1


class TestLoanRepositoryItemHistory:
    def test_list_for_item_newest_first(self, repo_session):
        w, item = _catalog(repo_session, _DUNE)
        p1 = _patron(repo_session, "Fran")
        p2 = _patron(repo_session, "Gina")
        l1 = _circ(repo_session).checkout(item.barcode, p1.library_card_number)
        _circ(repo_session).checkin_by_id(l1.id)
        l2 = _circ(repo_session).checkout(item.barcode, p2.library_card_number)
        repo_session.commit()
        history = SqlLoanRepository(repo_session).list_for_item(item.id)
        assert [l.id for l in history] == [l2.id, l1.id]


class TestFineRepositoryOutstanding:
    def test_list_outstanding_excludes_paid_and_waived(self, repo_session):
        p = _patron(repo_session, "Hank")
        fr = SqlFineRepository(repo_session)
        fr.add(Fine(
            patron_id=p.id, kind=FineKind.OTHER.value, amount_cents=500,
            status=FineStatus.OUTSTANDING.value,
        ))
        fr.add(Fine(
            patron_id=p.id, kind=FineKind.LOST.value, amount_cents=1500,
            status=FineStatus.PAID.value,
        ))
        fr.add(Fine(
            patron_id=p.id, kind=FineKind.DAMAGED.value, amount_cents=200,
            status=FineStatus.WAIVED.value,
        ))
        repo_session.commit()
        rows = fr.list_outstanding()
        # Filter to this patron's rows
        mine = [f for f in rows if f.patron_id == p.id]
        assert len(mine) == 1
        assert mine[0].amount_cents == 500

    def test_outstanding_total_all_sums(self, repo_session):
        p1 = _patron(repo_session, "Ivy")
        p2 = _patron(repo_session, "Jack")
        fr = SqlFineRepository(repo_session)
        fr.add(Fine(
            patron_id=p1.id, kind=FineKind.OTHER.value, amount_cents=100,
            status=FineStatus.OUTSTANDING.value,
        ))
        fr.add(Fine(
            patron_id=p2.id, kind=FineKind.OTHER.value, amount_cents=250,
            status=FineStatus.OUTSTANDING.value,
        ))
        repo_session.commit()
        total = fr.outstanding_total_all()
        # Includes other test data; just check it's at least 350
        assert total >= 350

    def test_list_outstanding_query_matches_patron(self, repo_session):
        p = _patron(repo_session, "Karen Queryable")
        fr = SqlFineRepository(repo_session)
        fr.add(Fine(
            patron_id=p.id, kind=FineKind.OTHER.value, amount_cents=100,
            status=FineStatus.OUTSTANDING.value,
        ))
        repo_session.commit()
        rows = fr.list_outstanding(query="Queryable")
        assert any(f.patron_id == p.id for f in rows)


# ──────────────────────────────────────────────────────────────────────────────
# Web: /ui/admin/loans, /ui/admin/fines, patron loans history, item history
# ──────────────────────────────────────────────────────────────────────────────


def _csrf_pair(settings: Settings) -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _derive_csrf_secret(settings.jwt_secret_key))}"


@pytest.fixture(scope="module")
def web_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def web_session(web_engine):
    factory = sessionmaker(bind=web_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def web_client(web_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: web_session
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


class TestAdminLoansPage:
    def test_librarian_sees_loans(self, web_client, web_session):
        w, i = _catalog(web_session, _DUNE)
        p = _patron(web_session, "Lara")
        _circ(web_session).checkout(i.barcode, p.library_card_number)
        web_session.commit()
        cookies = _login(web_client, web_session, "Librarian")
        resp = web_client.get("/ui/admin/loans", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Active loans" in body
        assert p.library_card_number in body
        assert i.barcode in body

    def test_patron_forbidden(self, web_client, web_session):
        cookies = _login(web_client, web_session, "Patron")
        resp = web_client.get("/ui/admin/loans", cookies=cookies)
        assert resp.status_code in (302, 303, 403)

    def test_due_overdue_filter(self, web_client, web_session):
        w, i = _catalog(web_session, _DUNE)
        p = _patron(web_session, "Mike")
        loan = _circ(web_session).checkout(i.barcode, p.library_card_number)
        loan.due_at = datetime.now(tz=timezone.utc) - timedelta(days=2)
        web_session.flush()
        web_session.commit()
        cookies = _login(web_client, web_session, "Librarian")
        resp = web_client.get("/ui/admin/loans?due=overdue", cookies=cookies)
        assert resp.status_code == 200
        assert i.barcode in resp.content.decode()


class TestAdminFinesPage:
    def test_librarian_sees_fines_with_total(self, web_client, web_session):
        p = _patron(web_session, "Nora")
        SqlFineRepository(web_session).add(Fine(
            patron_id=p.id, kind=FineKind.OTHER.value, amount_cents=750,
            status=FineStatus.OUTSTANDING.value,
        ))
        web_session.commit()
        cookies = _login(web_client, web_session, "Librarian")
        resp = web_client.get("/ui/admin/fines", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Outstanding fines" in body
        assert "total owed" in body
        assert p.library_card_number in body

    def test_patron_forbidden(self, web_client, web_session):
        cookies = _login(web_client, web_session, "Patron")
        resp = web_client.get("/ui/admin/fines", cookies=cookies)
        assert resp.status_code in (302, 303, 403)


class TestPatronLoanHistoryPage:
    def test_status_toggle_surfaces_returned(self, web_client, web_session):
        p = _patron(web_session, "Otis")
        w, i = _catalog(web_session, _DUNE)
        loan = _circ(web_session).checkout(i.barcode, p.library_card_number)
        _circ(web_session).checkin_by_id(loan.id)
        web_session.commit()
        cookies = _login(web_client, web_session, "Librarian")
        # Returned view
        resp = web_client.get(
            f"/ui/patrons/{p.library_card_number}/loans?status=returned",
            cookies=cookies,
        )
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Returned loans" in body  # heading reflects status
        # Returned mode renders a <th>Returned</th> column; active mode does not.
        assert "<th>Returned</th>" in body
        # Active default — should NOT show the now-returned loan
        resp = web_client.get(
            f"/ui/patrons/{p.library_card_number}/loans", cookies=cookies
        )
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Active loans" in body
        assert "No active loans" in body


class TestItemDetailLoanHistory:
    def test_librarian_sees_loan_history(self, web_client, web_session):
        p = _patron(web_session, "Pia")
        w, i = _catalog(web_session, _DUNE)
        _circ(web_session).checkout(i.barcode, p.library_card_number)
        web_session.commit()
        cookies = _login(web_client, web_session, "Librarian")
        resp = web_client.get(f"/ui/items/{i.barcode}", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Loan history" in body
        assert p.library_card_number in body

    def test_patron_does_not_see_loan_history(self, web_client, web_session):
        p = _patron(web_session, "Quinn")
        w, i = _catalog(web_session, _DUNE)
        _circ(web_session).checkout(i.barcode, p.library_card_number)
        web_session.commit()
        cookies = _login(web_client, web_session, "Patron")
        resp = web_client.get(f"/ui/items/{i.barcode}", cookies=cookies)
        assert resp.status_code == 200
        assert "Loan history" not in resp.content.decode()


# ──────────────────────────────────────────────────────────────────────────────
# API: GET /loans, /loans/patron/{card}, /loans/item/{barcode}, /fines
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


class TestLoanApiListViews:
    def test_list_active_requires_loan_view_any(self, api_client, api_session):
        token = _issue_token(api_session, "Patron")
        resp = api_client.get("/loans", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_list_active_as_librarian(self, api_client, api_session):
        w, i = _catalog(api_session, _DUNE)
        p = _patron(api_session, "Rae")
        _circ(api_session).checkout(i.barcode, p.library_card_number)
        api_session.commit()
        token = _issue_token(api_session, "Librarian")
        resp = api_client.get("/loans", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_patron_history_endpoint(self, api_client, api_session):
        w, i = _catalog(api_session, _DUNE)
        p = _patron(api_session, "Sam")
        loan = _circ(api_session).checkout(i.barcode, p.library_card_number)
        _circ(api_session).checkin_by_id(loan.id)
        api_session.commit()
        token = _issue_token(api_session, "Librarian")
        resp = api_client.get(
            f"/loans/patron/{p.library_card_number}?status=returned",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_item_history_endpoint(self, api_client, api_session):
        w, i = _catalog(api_session, _DUNE)
        p = _patron(api_session, "Tess")
        _circ(api_session).checkout(i.barcode, p.library_card_number)
        api_session.commit()
        token = _issue_token(api_session, "Librarian")
        resp = api_client.get(
            f"/loans/item/{i.barcode}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestFineApiSystemWide:
    def test_list_outstanding_requires_fine_manage(self, api_client, api_session):
        token = _issue_token(api_session, "Patron")
        resp = api_client.get("/fines", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_list_outstanding_as_librarian(self, api_client, api_session):
        p = _patron(api_session, "Una")
        SqlFineRepository(api_session).add(Fine(
            patron_id=p.id, kind=FineKind.OTHER.value, amount_cents=400,
            status=FineStatus.OUTSTANDING.value,
        ))
        api_session.commit()
        token = _issue_token(api_session, "Librarian")
        resp = api_client.get("/fines", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert any(f["amount_cents"] == 400 for f in resp.json())
