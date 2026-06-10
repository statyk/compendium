"""Shared fixtures for the /ui/scan/* integration tests.

pytest auto-discovers fixtures defined here for every test under
``tests/integration/``. Pure helper functions live in ``scan_helpers.py``.
"""

from __future__ import annotations

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
from compendium.domain.models import Base
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)

# A loopback public_base_url so resolve_public_base_url accepts the test request.
_BASE_URL = "https://library.example.org"


@pytest.fixture(scope="module")
def scan_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def scan_session(scan_engine, monkeypatch):
    factory = sessionmaker(bind=scan_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    # The site-settings cache reads from its own lazy engine (not this in-memory
    # test engine), so set public_base_url via env — env wins on read and
    # bypasses the cache. This lets resolve_public_base_url pass the HTTPS gate.
    from compendium.services.site_settings import invalidate_cache

    monkeypatch.setenv("COMPENDIUM_PUBLIC_BASE_URL", _BASE_URL)
    invalidate_cache()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def scan_client(scan_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: scan_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c
