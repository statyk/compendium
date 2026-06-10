"""Integration tests for scan_event emission on /ui/scan/dispatch."""

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
from compendium.domain.identifiers import format_item_barcode, format_patron_card
from compendium.domain.models import (
    AppUser,
    Base,
    Item,
    Loan,
    MediaType,
    Patron,
    ScanEvent,
    ScanPairing,
    Work,
)
from compendium.repositories.sql.branch_repository import SqlBranchRepository
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
        username=f"feedstaff{_next()}", password_hash=hash_password("x"), role_id=role.id
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


def _book(session, title="Dune") -> Item:
    mt = session.query(MediaType).filter_by(code="book").one()
    w = Work(title=title, media_type_id=mt.id)
    session.add(w)
    session.flush()
    branch = SqlBranchRepository(session).get_default()
    n = _next()
    it = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode=format_item_barcode(f"{n:08d}", location_code=None),
        accession_number=f"FACC{n:06d}",
    )
    session.add(it)
    session.flush()
    return it


def _patron(session) -> Patron:
    n = _next()
    p = Patron(
        library_card_number=format_patron_card(f"{n:08d}", location_code=None),
        full_name=f"Feed Patron {n}",
        is_active=True,
    )
    session.add(p)
    session.flush()
    return p


def _events(session, row):
    return (
        session.query(ScanEvent)
        .filter(ScanEvent.pairing_id == row.id)
        .order_by(ScanEvent.id)
        .all()
    )


def test_checkout_writes_event(scan_client, scan_session):
    user = _staff_user(scan_session)
    row, scan_cookie = _claim(
        scan_client, scan_session, user, allowed_modes=["checkout"], mode="checkout"
    )
    patron = _patron(scan_session)
    item = _book(scan_session, title="FeedDuneBook")

    raw, signed = _csrf_pair()
    cookies = {SCAN_COOKIE: scan_cookie, CSRF_COOKIE: signed}
    scan_client.post(
        "/ui/scan/dispatch",
        data={"code": patron.library_card_number, "csrf_token": raw},
        cookies=cookies,
    )
    scan_client.post(
        "/ui/scan/dispatch",
        data={"code": item.barcode, "csrf_token": raw},
        cookies=cookies,
    )

    events = _events(scan_session, row)
    # borrower_set + checkout both write events.
    checkout_evt = [e for e in events if e.message.startswith("Checked out")]
    assert len(checkout_evt) == 1
    evt = checkout_evt[0]
    assert evt.kind == "ok"
    assert evt.item_id == item.id
    assert evt.patron_id == patron.id


def test_duplicate_scan_writes_no_event(scan_client, scan_session):
    user = _staff_user(scan_session)
    row, scan_cookie = _claim(
        scan_client, scan_session, user, allowed_modes=["checkin"], mode="checkin"
    )
    patron = _patron(scan_session)
    item = _book(scan_session)
    scan_session.add(
        Loan(
            item_id=item.id,
            patron_id=patron.id,
            branch_id=item.branch_id,
            due_at=datetime.now(timezone.utc) + timedelta(days=14),
        )
    )
    item.status = "checked_out"
    scan_session.flush()

    raw, signed = _csrf_pair()
    cookies = {SCAN_COOKIE: scan_cookie, CSRF_COOKIE: signed}
    r1 = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": item.barcode, "csrf_token": raw},
        cookies=cookies,
    )
    assert r1.json()["kind"] == "checkin"
    before = len(_events(scan_session, row))
    r2 = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": item.barcode, "csrf_token": raw},
        cookies=cookies,
    )
    assert r2.json()["kind"] == "ignored"
    # No new event for the ignored duplicate.
    assert len(_events(scan_session, row)) == before


def test_error_scan_writes_error_event(scan_client, scan_session):
    """An item scan in checkout mode with no borrower set is an error."""
    user = _staff_user(scan_session)
    row, scan_cookie = _claim(
        scan_client, scan_session, user, allowed_modes=["checkout"], mode="checkout"
    )
    item = _book(scan_session)
    raw, signed = _csrf_pair()
    resp = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": item.barcode, "csrf_token": raw},
        cookies={SCAN_COOKIE: scan_cookie, CSRF_COOKIE: signed},
    )
    assert resp.json()["kind"] == "error"
    events = _events(scan_session, row)
    assert len(events) == 1
    assert events[0].kind == "error"
