"""Tests for HSTS and TrustedHostMiddleware (M5/M8 security fixes)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.db.session import get_session
from tests.helpers import make_engine, session_for, std_settings


@pytest.fixture(scope="module")
def engine():
    return make_engine()


@pytest.fixture
def db_session(engine):
    yield from session_for(engine)


@pytest.fixture
def client(engine, db_session):
    app = create_app()
    from sqlalchemy.orm import sessionmaker
    fac = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        s = fac()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_session] = _override
    settings = std_settings()
    with patch("compendium.db.engine.get_settings", return_value=settings):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


class TestHSTS:
    def test_no_hsts_on_http(self, client):
        resp = client.get("/auth/me")
        assert "strict-transport-security" not in resp.headers

    def test_hsts_present_on_https(self, engine, db_session):
        app = create_app()
        from sqlalchemy.orm import sessionmaker
        fac = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        def _override():
            s = fac()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        app.dependency_overrides[get_session] = _override
        settings = std_settings()
        with patch("compendium.db.engine.get_settings", return_value=settings):
            with TestClient(
                app,
                raise_server_exceptions=True,
                follow_redirects=False,
                base_url="https://testserver",
            ) as c:
                resp = c.get("/auth/me")
        assert "strict-transport-security" in resp.headers
        assert "max-age=63072000" in resp.headers["strict-transport-security"]


class TestTrustedHost:
    def test_any_host_allowed_when_not_configured(self, client):
        # No allowed_hosts configured → any Host is accepted.
        resp = client.get("/auth/me")
        # /auth/me returns 401 (not authenticated) or 404, never 400 from TrustedHostMiddleware
        assert resp.status_code in (401, 404)

    def test_allowed_host_passes(self, engine, db_session):
        from sqlalchemy.orm import sessionmaker
        fac = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        def _override():
            s = fac()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        # Include "testserver" so the default TestClient base_url is allowed.
        settings = std_settings(allowed_hosts="library.example.org,testserver")
        with patch("compendium.api.app.get_settings", return_value=settings):
            app = create_app()
            app.dependency_overrides[get_session] = _override
            with TestClient(
                app, raise_server_exceptions=True, follow_redirects=False
            ) as c:
                resp = c.get("/auth/me")
        assert resp.status_code in (401, 404)  # reached the app, not blocked

    def test_disallowed_host_rejected(self, engine, db_session):
        from sqlalchemy.orm import sessionmaker
        fac = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        def _override():
            s = fac()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        settings = std_settings(allowed_hosts="library.example.org")
        with patch("compendium.api.app.get_settings", return_value=settings):
            app = create_app()
            app.dependency_overrides[get_session] = _override
            # Use evil.example.com as base_url so httpx sends Host: evil.example.com
            with TestClient(
                app,
                raise_server_exceptions=False,
                follow_redirects=False,
                base_url="http://evil.example.com",
            ) as c:
                resp = c.get("/auth/me")
        assert resp.status_code == 400
