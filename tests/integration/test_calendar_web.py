"""Integration tests for library-hours and closed-dates web UI routes."""
from __future__ import annotations

import re
from datetime import date, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base, ClosedDate
from compendium.repositories.sql.calendar_repository import (
    SqlClosedDateRepository,
    SqlLibraryHoursRepository,
)
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _derive_csrf_secret(_SECRET))}"


@pytest.fixture(scope="module")
def calw_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def calw_session(calw_engine):
    factory = sessionmaker(bind=calw_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def calw_client(calw_session):
    from unittest.mock import patch
    app = create_app()
    app.dependency_overrides[get_session] = lambda: calw_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


def _login(client, session, username, role_name) -> dict:
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
    assert resp.status_code in (200, 302, 303), f"Login failed: {resp.status_code}"
    return {"cookies": resp.cookies}


def _get_csrf(client, url: str, cookies: dict) -> str:
    resp = client.get(url, cookies=cookies)
    assert resp.status_code == 200
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
    assert m, f"CSRF token not found on {url}"
    return m.group(1)


class TestLibraryHoursWeb:
    def test_get_library_hours_page(self, calw_client, calw_session):
        info = _login(calw_client, calw_session, "lib_hours_get", "Librarian")
        resp = calw_client.get("/ui/admin/library-hours", cookies=info["cookies"])
        assert resp.status_code == 200
        assert "Library Hours" in resp.text
        assert "Monday" in resp.text

    def test_update_weekday_closes_sunday(self, calw_client, calw_session):
        info = _login(calw_client, calw_session, "lib_hours_upd", "Librarian")
        csrf = _get_csrf(calw_client, "/ui/admin/library-hours", info["cookies"])
        resp = calw_client.post(
            "/ui/admin/library-hours/6/update",
            data={"is_open": "", "csrf_token": csrf},
            cookies=info["cookies"],
        )
        assert resp.status_code in (302, 303)
        row = SqlLibraryHoursRepository(calw_session).get(6)
        assert row.is_open is False

    def test_update_weekday_sets_times(self, calw_client, calw_session):
        info = _login(calw_client, calw_session, "lib_hours_time", "Librarian")
        csrf = _get_csrf(calw_client, "/ui/admin/library-hours", info["cookies"])
        resp = calw_client.post(
            "/ui/admin/library-hours/0/update",
            data={"is_open": "on", "open_time": "09:00", "close_time": "17:00", "csrf_token": csrf},
            cookies=info["cookies"],
        )
        assert resp.status_code in (302, 303)
        row = SqlLibraryHoursRepository(calw_session).get(0)
        assert row.is_open is True
        assert row.close_time == time(17, 0)

    def test_update_without_permission_returns_403(self, calw_client, calw_session):
        info = _login(calw_client, calw_session, "patron_hrs", "Patron")
        resp = calw_client.get("/ui/admin/library-hours", cookies=info["cookies"])
        assert resp.status_code == 403


class TestClosedDatesWeb:
    def test_get_closed_dates_page(self, calw_client, calw_session):
        info = _login(calw_client, calw_session, "lib_cd_get", "Librarian")
        resp = calw_client.get("/ui/admin/closed-dates", cookies=info["cookies"])
        assert resp.status_code == 200
        assert "Closed Dates" in resp.text

    def test_add_closed_date(self, calw_client, calw_session):
        info = _login(calw_client, calw_session, "lib_cd_add", "Librarian")
        csrf = _get_csrf(calw_client, "/ui/admin/closed-dates", info["cookies"])
        resp = calw_client.post(
            "/ui/admin/closed-dates/new",
            data={
                "start_date": "2026-12-25",
                "label": "Christmas",
                "recurs_annually": "on",
                "csrf_token": csrf,
            },
            cookies=info["cookies"],
        )
        assert resp.status_code in (302, 303)
        dates = SqlClosedDateRepository(calw_session).list()
        assert any(cd.label == "Christmas" and cd.recurs_annually for cd in dates)

    def test_delete_closed_date(self, calw_client, calw_session):
        cd = ClosedDate(start_date=date(2026, 7, 4), end_date=date(2026, 7, 4), label="Delete Me")
        calw_session.add(cd)
        calw_session.flush()
        cd_id = cd.id

        info = _login(calw_client, calw_session, "lib_cd_del", "Librarian")
        csrf = _get_csrf(calw_client, "/ui/admin/closed-dates", info["cookies"])
        resp = calw_client.post(
            f"/ui/admin/closed-dates/{cd_id}/delete",
            data={"csrf_token": csrf},
            cookies=info["cookies"],
        )
        assert resp.status_code in (302, 303)
        assert SqlClosedDateRepository(calw_session).get(cd_id) is None
