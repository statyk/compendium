"""Web UI tests for /ui/kiosk self-checkout."""

from __future__ import annotations

from datetime import date, timedelta
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
from compendium.domain.enums import ItemStatus
from compendium.domain.models import AppUser, Base, Item, Loan, MediaType, Patron, Work
from compendium.domain.models import LoanPolicy
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
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
def kiosk_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def kiosk_session(kiosk_engine):
    factory = sessionmaker(bind=kiosk_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def kiosk_client(kiosk_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: kiosk_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


def _login_kiosk(client, session, username="kiosk1", role_name="Librarian") -> dict:
    """Log in as a user with loan.checkout permission."""
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
    assert resp.status_code == 303
    return dict(resp.cookies)


def _book(session, title="Dune") -> Item:
    from compendium.repositories.sql.patron_repository import SqlPatronRepository  # noqa

    mt = session.query(MediaType).filter_by(code="book").one()
    w = Work(title=title, media_type_id=mt.id)
    session.add(w)
    session.flush()
    branch = SqlBranchRepository(session).get_default()
    n = _next()
    it = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode=f"KBC{n:06d}",
        accession_number=f"KACC{n:06d}",
    )
    session.add(it)
    session.flush()
    return it


_n = {"i": 0}


def _next() -> int:
    _n["i"] += 1
    return _n["i"]


def _patron(session, *, active=True, expires_at=None) -> Patron:
    n = _next()
    p = Patron(
        library_card_number=f"KCARD{n:04d}",
        full_name=f"Kiosk Patron {n}",
        is_active=active,
        expires_at=expires_at,
    )
    session.add(p)
    session.flush()
    return p


class TestAuth:
    def test_kiosk_requires_loan_checkout(self, kiosk_client, kiosk_session):
        cookies = _login_kiosk(kiosk_client, kiosk_session, "ro1", "ReadOnly")
        resp = kiosk_client.get("/ui/kiosk", cookies=cookies)
        assert resp.status_code == 403

    def test_kiosk_landing_renders(self, kiosk_client, kiosk_session):
        cookies = _login_kiosk(kiosk_client, kiosk_session, "kl1")
        resp = kiosk_client.get("/ui/kiosk", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Self-Checkout" in body
        assert 'name="card_number"' in body
        # Kiosk base template should NOT include admin nav
        assert "/ui/admin/import" not in body
        assert "Audit Log" not in body


class TestStart:
    def test_start_unknown_card(self, kiosk_client, kiosk_session):
        cookies = _login_kiosk(kiosk_client, kiosk_session, "kl2")
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = kiosk_client.post(
            "/ui/kiosk/start",
            data={"card_number": "NOSUCH", "csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 303
        assert "Card+not+recognized" in resp.headers["location"] or "Card%20not%20recognized" in resp.headers["location"]

    def test_start_redirects_to_session(self, kiosk_client, kiosk_session):
        cookies = _login_kiosk(kiosk_client, kiosk_session, "kl3")
        p = _patron(kiosk_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = kiosk_client.post(
            "/ui/kiosk/start",
            data={"card_number": p.library_card_number, "csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/ui/kiosk/session/{p.library_card_number}"

    def test_start_inactive_patron(self, kiosk_client, kiosk_session):
        cookies = _login_kiosk(kiosk_client, kiosk_session, "kl4")
        p = _patron(kiosk_session, active=False)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = kiosk_client.post(
            "/ui/kiosk/start",
            data={"card_number": p.library_card_number, "csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 303
        assert "not+active" in resp.headers["location"] or "not%20active" in resp.headers["location"]

    def test_start_expired_patron(self, kiosk_client, kiosk_session):
        cookies = _login_kiosk(kiosk_client, kiosk_session, "kl5")
        p = _patron(kiosk_session, expires_at=date.today() - timedelta(days=1))
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = kiosk_client.post(
            "/ui/kiosk/start",
            data={"card_number": p.library_card_number, "csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 303
        assert "not+active" in resp.headers["location"] or "not%20active" in resp.headers["location"]


class TestSessionPage:
    def test_session_renders_patron_name_and_scanner(self, kiosk_client, kiosk_session):
        cookies = _login_kiosk(kiosk_client, kiosk_session, "kl6")
        p = _patron(kiosk_session)
        resp = kiosk_client.get(
            f"/ui/kiosk/session/{p.library_card_number}", cookies=cookies
        )
        assert resp.status_code == 200
        body = resp.content.decode()
        assert p.full_name in body
        assert 'name="barcode"' in body
        assert "checkout-list" in body

    def test_session_redirects_when_card_unknown(self, kiosk_client, kiosk_session):
        cookies = _login_kiosk(kiosk_client, kiosk_session, "kl7")
        resp = kiosk_client.get("/ui/kiosk/session/NOSUCH", cookies=cookies)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/ui/kiosk?error=")


class TestCheckoutPost:
    def test_successful_checkout_returns_success_and_oob_list_row(
        self, kiosk_client, kiosk_session
    ):
        cookies = _login_kiosk(kiosk_client, kiosk_session, "kl8")
        p = _patron(kiosk_session)
        item = _book(kiosk_session, title="TestKioskBook")
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = kiosk_client.post(
            f"/ui/kiosk/session/{p.library_card_number}/checkout",
            data={"barcode": item.barcode, "csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 200
        body = resp.text
        assert "success-banner" in body
        assert "TestKioskBook" in body
        assert "hx-swap-oob" in body
        # Verify the loan actually landed
        from compendium.repositories.sql.loan_repository import SqlLoanRepository

        loan = SqlLoanRepository(kiosk_session).get_active_for_item(item.id)
        assert loan is not None
        assert loan.patron_id == p.id

    def test_unknown_barcode_shows_friendly_error(self, kiosk_client, kiosk_session):
        cookies = _login_kiosk(kiosk_client, kiosk_session, "kl9")
        p = _patron(kiosk_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = kiosk_client.post(
            f"/ui/kiosk/session/{p.library_card_number}/checkout",
            data={"barcode": "NOITEM", "csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert "error-banner" in resp.text
        assert "Item not found" in resp.text

    def test_already_checked_out_shows_friendly_error(
        self, kiosk_client, kiosk_session
    ):
        cookies = _login_kiosk(kiosk_client, kiosk_session, "kl10")
        p1 = _patron(kiosk_session)
        p2 = _patron(kiosk_session)
        item = _book(kiosk_session)
        # First patron checks it out
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        kiosk_client.post(
            f"/ui/kiosk/session/{p1.library_card_number}/checkout",
            data={"barcode": item.barcode, "csrf_token": raw},
            cookies=cookies,
        )
        # Second tries to check out the same item
        resp = kiosk_client.post(
            f"/ui/kiosk/session/{p2.library_card_number}/checkout",
            data={"barcode": item.barcode, "csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert "error-banner" in resp.text
        assert "currently checked out" in resp.text

    def test_blocked_by_fines_shows_friendly_error(
        self, kiosk_client, kiosk_session, monkeypatch
    ):
        # Configure a low fine block threshold + assess an overdue-blocking fine.
        from compendium.domain.enums import FineKind, FineStatus
        from compendium.domain.models import Fine
        from compendium.services import site_settings as _ss

        # Threshold lookup now goes through get_site_setting; env wins.
        monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_THRESHOLD_CENTS", "100")
        _ss.invalidate_cache()
        cookies = _login_kiosk(kiosk_client, kiosk_session, "kl11")
        p = _patron(kiosk_session)
        item = _book(kiosk_session)
        # Record a 500¢ outstanding fine so the patron is blocked.
        kiosk_session.add(
            Fine(
                patron_id=p.id,
                kind=FineKind.OTHER.value,
                amount_cents=500,
                status=FineStatus.OUTSTANDING.value,
            )
        )
        kiosk_session.commit()

        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = kiosk_client.post(
            "/ui/kiosk/start",
            data={"card_number": p.library_card_number, "csrf_token": raw},
            cookies=cookies,
        )
        assert resp.status_code == 303
        # Should bounce back to landing with outstanding-fees message
        assert (
            "outstanding+fees" in resp.headers["location"]
            or "outstanding%20fees" in resp.headers["location"]
        )


# ── UI polish — Slice A (kiosk scan buttons) ──────────────────────────────────


def test_kiosk_landing_has_scan_button(kiosk_client, kiosk_session):
    """Kiosk landing page renders a Scan button targeting the card input."""
    cookies = _login_kiosk(kiosk_client, kiosk_session, "kl_scan_landing")
    resp = kiosk_client.get("/ui/kiosk", cookies=cookies)
    assert resp.status_code == 200
    assert b'data-scan-target="card-input"' in resp.content


def test_kiosk_session_has_scan_button(kiosk_client, kiosk_session):
    """Kiosk session page renders a Scan button targeting the barcode input."""
    cookies = _login_kiosk(kiosk_client, kiosk_session, "kl_scan_session")
    patron = _patron(kiosk_session)
    resp = kiosk_client.get(
        f"/ui/kiosk/session/{patron.library_card_number}", cookies=cookies
    )
    assert resp.status_code == 200
    assert b'data-scan-target="barcode-input"' in resp.content


def test_kiosk_session_mentions_isbn_when_enabled(kiosk_client, kiosk_session):
    """Kiosk session page label includes ISBN when circulation_scan_isbn_enabled is true."""
    cookies = _login_kiosk(kiosk_client, kiosk_session, "kl_isbn_session")
    patron = _patron(kiosk_session)
    resp = kiosk_client.get(
        f"/ui/kiosk/session/{patron.library_card_number}", cookies=cookies
    )
    assert resp.status_code == 200
    assert b"Item barcode / ISBN" in resp.content


def test_kiosk_isbn_checkout_all_copies_out_friendly_error(
    kiosk_client, kiosk_session
):
    """The 'No available copy ...' service error maps to patron-friendly text."""
    cookies = _login_kiosk(kiosk_client, kiosk_session, "kl_isbn_allout")
    p1 = _patron(kiosk_session)
    p2 = _patron(kiosk_session)
    item = _book(kiosk_session)
    item.work.isbn = "9780441013593"
    kiosk_session.flush()
    raw, signed = _csrf_pair()
    cookies[CSRF_COOKIE] = signed
    kiosk_client.post(
        f"/ui/kiosk/session/{p1.library_card_number}/checkout",
        data={"barcode": item.barcode, "csrf_token": raw},
        cookies=cookies,
    )
    resp = kiosk_client.post(
        f"/ui/kiosk/session/{p2.library_card_number}/checkout",
        data={"barcode": "9780441013593", "csrf_token": raw},
        cookies=cookies,
    )
    assert resp.status_code == 200
    assert "error-banner" in resp.text
    assert "All copies of this title are currently checked out." in resp.text
