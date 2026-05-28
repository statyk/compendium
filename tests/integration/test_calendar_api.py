"""Integration tests for library-hours and closed-dates REST API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.models import Base
from compendium.repositories.sql.calendar_repository import (
    SqlClosedDateRepository,
    SqlLibraryHoursRepository,
)
from tests.helpers import make_user, setup_sqlite_fts, std_settings

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)


@pytest.fixture(scope="module")
def cala_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def cala_session(cala_engine):
    from unittest.mock import patch
    factory = sessionmaker(bind=cala_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def cala_client(cala_session):
    from unittest.mock import patch
    app = create_app()
    app.dependency_overrides[get_session] = lambda: cala_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


def _lib_headers(client, session) -> dict:
    _, token = make_user(session, "api_lib_user", "Librarian")
    return {"Authorization": f"Bearer {token}"}


class TestLibraryHoursApi:
    def test_list_hours_returns_seven_rows(self, cala_client, cala_session):
        headers = _lib_headers(cala_client, cala_session)
        resp = cala_client.get("/library-hours/", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 7
        assert all("weekday" in row and "is_open" in row for row in data)

    def test_patch_weekday_closes_sunday(self, cala_client, cala_session):
        headers = _lib_headers(cala_client, cala_session)
        resp = cala_client.patch("/library-hours/6", json={"is_open": False}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["is_open"] is False
        assert SqlLibraryHoursRepository(cala_session).get(6).is_open is False

    def test_patch_weekday_sets_close_time(self, cala_client, cala_session):
        headers = _lib_headers(cala_client, cala_session)
        resp = cala_client.patch(
            "/library-hours/1",
            json={"is_open": True, "close_time": "17:00:00"},
            headers=headers,
        )
        assert resp.status_code == 200
        row = SqlLibraryHoursRepository(cala_session).get(1)
        assert row.close_time.strftime("%H:%M") == "17:00"

    def test_patch_invalid_weekday_returns_404(self, cala_client, cala_session):
        headers = _lib_headers(cala_client, cala_session)
        resp = cala_client.patch("/library-hours/7", json={"is_open": False}, headers=headers)
        assert resp.status_code in (404, 422)

    def test_requires_authentication(self, cala_client):
        resp = cala_client.get("/library-hours/")
        assert resp.status_code == 401


class TestClosedDatesApi:
    def test_create_closed_date(self, cala_client, cala_session):
        headers = _lib_headers(cala_client, cala_session)
        resp = cala_client.post(
            "/closed-dates/",
            json={"start_date": "2026-12-25", "label": "Christmas", "recurs_annually": True},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["label"] == "Christmas"
        assert data["recurs_annually"] is True
        assert data["end_date"] == "2026-12-25"

    def test_list_closed_dates(self, cala_client, cala_session):
        headers = _lib_headers(cala_client, cala_session)
        # Add one first
        cala_client.post(
            "/closed-dates/",
            json={"start_date": "2026-07-04", "label": "Independence Day"},
            headers=headers,
        )
        resp = cala_client.get("/closed-dates/", headers=headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_delete_closed_date(self, cala_client, cala_session):
        headers = _lib_headers(cala_client, cala_session)
        resp = cala_client.post(
            "/closed-dates/",
            json={"start_date": "2026-11-26", "label": "Thanksgiving"},
            headers=headers,
        )
        cd_id = resp.json()["id"]
        resp = cala_client.delete(f"/closed-dates/{cd_id}", headers=headers)
        assert resp.status_code == 204
        assert SqlClosedDateRepository(cala_session).get(cd_id) is None

    def test_delete_nonexistent_returns_404(self, cala_client, cala_session):
        headers = _lib_headers(cala_client, cala_session)
        resp = cala_client.delete("/closed-dates/9999", headers=headers)
        assert resp.status_code == 404
