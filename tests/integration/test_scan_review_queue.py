"""Integration tests for review-first catalog scanning (/ui/scan/*)."""

from __future__ import annotations

import hashlib
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
from compendium.domain.models import (
    AppUser,
    Base,
    Item,
    ScanPairing,
    ScanPendingItem,
)
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from compendium.web.deps import SCAN_COOKIE
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)
_BASE_URL = "https://library.example.org"

_GIVER_ISBN = "9780544336261"
_GIVER_META = {"title": "The Giver", "isbn": _GIVER_ISBN}
# A second ISBN for the review-off test, so it can't collide with the review-on
# test on the per-process idempotency guard (keyed on pairing+mode+code).
_OFF_ISBN = "9780571056866"


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _derive_csrf_secret(_SECRET))}"


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


_n = {"i": 0}


def _next() -> int:
    _n["i"] += 1
    return _n["i"]


def _staff_user(session, role_name="Librarian"):
    role = SqlRoleRepository(session).get_by_name(role_name)
    u = AppUser(
        username=f"revstaff{_next()}", password_hash=hash_password("x"), role_id=role.id
    )
    SqlUserRepository(session).add(u)
    session.flush()
    return u


def _make_pairing(session, user, *, claim, allowed_modes, mode=None, ttl_minutes=2):
    now = datetime.now(timezone.utc)
    row = ScanPairing(
        token_hash=hashlib.sha256(claim.encode()).hexdigest(),
        user_id=user.id,
        allowed_modes=allowed_modes,
        mode=mode or allowed_modes[0],
        count=0,
        created_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    session.add(row)
    session.flush()
    return row


def _claim(client, session, user, *, allowed_modes, mode=None):
    claim = f"CLAIM_{_next()}"
    row = _make_pairing(session, user, claim=claim, allowed_modes=allowed_modes, mode=mode)
    resp = client.get(f"/ui/scan/pair?c={claim}")
    assert resp.status_code == 200
    return row, resp.cookies[SCAN_COOKIE]


@pytest.fixture(autouse=True)
def _stub_metadata(monkeypatch):
    """No network: stub the upstream metadata + cover lookups."""
    monkeypatch.setattr(
        "compendium.services.catalog.lookup_metadata",
        lambda *a, **k: dict(_GIVER_META),
    )
    monkeypatch.setattr(
        "compendium.services.catalog.lookup_cover_fallbacks",
        lambda *a, **k: None,
    )


def test_review_on_queues_pending_no_item(scan_client, scan_session):
    user = _staff_user(scan_session)
    row, scan_cookie = _claim(
        scan_client, scan_session, user, allowed_modes=["catalog"], mode="catalog"
    )
    row.catalog_review = True
    scan_session.flush()

    items_before = scan_session.query(Item).count()
    raw, signed = _csrf_pair()
    resp = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": _GIVER_ISBN, "csrf_token": raw},
        cookies={SCAN_COOKIE: scan_cookie, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "catalog_queued"

    pending = (
        scan_session.query(ScanPendingItem)
        .filter(ScanPendingItem.pairing_id == row.id)
        .all()
    )
    assert len(pending) == 1
    assert pending[0].status == "pending"
    assert pending[0].title == "The Giver"
    assert pending[0].isbn == _GIVER_ISBN
    # No Item was created.
    assert scan_session.query(Item).count() == items_before


def test_review_off_adds_item(scan_client, scan_session):
    user = _staff_user(scan_session)
    row, scan_cookie = _claim(
        scan_client, scan_session, user, allowed_modes=["catalog"], mode="catalog"
    )
    # catalog_review defaults to False.
    items_before = scan_session.query(Item).count()
    raw, signed = _csrf_pair()
    resp = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": _OFF_ISBN, "csrf_token": raw},
        cookies={SCAN_COOKIE: scan_cookie, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "catalog_added"
    # An Item was created; no pending row.
    assert scan_session.query(Item).count() == items_before + 1
    assert (
        scan_session.query(ScanPendingItem)
        .filter(ScanPendingItem.pairing_id == row.id)
        .count()
        == 0
    )


def test_review_toggle_flips_flag(scan_client, scan_session):
    user = _staff_user(scan_session)
    row, scan_cookie = _claim(
        scan_client, scan_session, user, allowed_modes=["catalog"], mode="catalog"
    )
    assert row.catalog_review is False
    raw, signed = _csrf_pair()
    resp = scan_client.post(
        "/ui/scan/review",
        data={"enabled": "1", "csrf_token": raw},
        cookies={SCAN_COOKIE: scan_cookie, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    assert resp.json()["catalog_review"] is True
    scan_session.refresh(row)
    assert row.catalog_review is True


def test_review_toggle_without_catalog_mode_400(scan_client, scan_session):
    user = _staff_user(scan_session)
    row, scan_cookie = _claim(
        scan_client, scan_session, user, allowed_modes=["checkout"], mode="checkout"
    )
    raw, signed = _csrf_pair()
    resp = scan_client.post(
        "/ui/scan/review",
        data={"enabled": "1", "csrf_token": raw},
        cookies={SCAN_COOKIE: scan_cookie, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 400
