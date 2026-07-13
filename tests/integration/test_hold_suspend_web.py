"""Web UI tests for hold suspend/resume self-service."""

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
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.auth import hash_password
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
_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _derive_csrf_secret(_SECRET))}"


@pytest.fixture(scope="module")
def hw_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def hw_session(hw_engine):
    factory = sessionmaker(bind=hw_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def hw_client(hw_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: hw_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


_n = {"i": 0}


def _next() -> int:
    _n["i"] += 1
    return _n["i"]


def _login_patron(client, session, title: str = "Dune") -> tuple[dict, Patron, Hold]:
    n = _next()
    # Create user + linked patron + waiting hold
    p_role = SqlRoleRepository(session).get_by_name("Patron")
    u = AppUser(
        username=f"hwp{n}", password_hash=hash_password("secret"), role_id=p_role.id
    )
    SqlUserRepository(session).add(u)
    session.flush()
    patron = Patron(
        library_card_number=f"HW{n:05d}", full_name="Alice", user_id=u.id
    )
    SqlPatronRepository(session).add(patron)
    session.flush()
    # Seed a book + another patron who checks it out, then our patron places a hold
    isbn = f"9780441{_next():06d}"
    work_meta = dict(_DUNE, title=title)
    with patch("compendium.services.metadata.lookup_isbn", return_value=work_meta):
        catalog = CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        )
        work, item = catalog.add_from_isbn(isbn)
    holder = Patron(library_card_number=f"HOLD{_next():05d}", full_name="Holder")
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
    holds_svc = HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
    )
    hold = holds_svc.place(work.id, patron.library_card_number)
    session.flush()
    session.commit()

    raw, signed = _csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": u.username, "password": "secret", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    return dict(resp.cookies), patron, hold


class TestMyHoldsShowsSuspendUI:
    def test_suspend_button_visible_for_waiting_hold(self, hw_client, hw_session):
        cookies, _, _hold = _login_patron(hw_client, hw_session)
        resp = hw_client.get("/ui/me/holds", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        # The suspend form with date input should appear
        assert 'name="until"' in body
        assert ">Suspend</button>" in body


class TestSuspendRoute:
    def test_suspend_via_form(self, hw_client, hw_session):
        cookies, patron, hold = _login_patron(hw_client, hw_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        until = (date.today() + timedelta(days=14)).isoformat()
        resp = hw_client.post(
            f"/ui/me/holds/{hold.id}/suspend",
            data={"until": until, "reason": "vacation", "csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert f'id="hold-{hold.id}"' in resp.text
        assert f"Suspended until {until}" in resp.text
        hw_session.expire_all()
        fresh = SqlHoldRepository(hw_session).get(hold.id)
        assert fresh.suspended_until == date.fromisoformat(until)
        assert fresh.suspended_reason == "vacation"

    def test_invalid_date_shows_error(self, hw_client, hw_session):
        cookies, _, hold = _login_patron(hw_client, hw_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = hw_client.post(
            f"/ui/me/holds/{hold.id}/suspend",
            data={"until": "not-a-date", "csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert "error-banner" in resp.text
        assert f'id="hold-{hold.id}"' in resp.text


class TestResumeRoute:
    def test_resume_clears_suspension(self, hw_client, hw_session):
        cookies, _, hold = _login_patron(hw_client, hw_session)
        # Pre-suspend
        hold.suspended_until = date.today() + timedelta(days=7)
        hw_session.flush()
        hw_session.commit()
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = hw_client.post(
            f"/ui/me/holds/{hold.id}/resume",
            data={"csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 200
        hw_session.expire_all()
        fresh = SqlHoldRepository(hw_session).get(hold.id)
        assert fresh.suspended_until is None

    def test_resume_rerenders_row_with_updated_state(self, hw_client, hw_session):
        cookies, _, hold = _login_patron(hw_client, hw_session)
        hold.suspended_until = date.today() + timedelta(days=7)
        hw_session.flush()
        hw_session.commit()
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = hw_client.post(
            f"/ui/me/holds/{hold.id}/resume",
            data={"csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 200
        body = resp.text
        assert f'id="hold-{hold.id}"' in body
        assert "Refresh to see updated state" not in body
        assert "Suspended until" not in body
        assert "/cancel" in body

    def test_resume_failure_preserves_row(self, hw_client, hw_session):
        _, _, _own_hold = _login_patron(hw_client, hw_session)
        other_cookies, _, other_hold = _login_patron(
            hw_client, hw_session, title="Foundation"
        )
        cookies, _, _ = _login_patron(hw_client, hw_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = hw_client.post(
            f"/ui/me/holds/{other_hold.id}/resume",
            data={"csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert "error-banner" in resp.text
        assert "Foundation" not in resp.text


class TestCancelRoute:
    def test_cancel_failure_preserves_row(self, hw_client, hw_session):
        cookies, _, _hold = _login_patron(hw_client, hw_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = hw_client.post(
            "/ui/me/holds/999999/cancel",
            data={"csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert "error-banner" in resp.text
