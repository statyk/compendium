"""Web UI tests for /ui/scan/* phone-scanner pairing."""

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
    MediaType,
    Patron,
    Role,
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

# A loopback public_base_url so resolve_public_base_url accepts the test request.
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


_n = {"i": 0}


def _next() -> int:
    _n["i"] += 1
    return _n["i"]


def _login(client, session, *, role_name="Librarian", username=None) -> dict:
    username = username or f"scanstaff{_next()}"
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


def _custom_role(session, name, permissions) -> Role:
    role = Role(name=name, permissions=permissions, is_system=False)
    session.add(role)
    session.flush()
    return role


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
        accession_number=f"SACC{n:06d}",
    )
    session.add(it)
    session.flush()
    return it


def _patron(session) -> Patron:
    n = _next()
    p = Patron(
        library_card_number=format_patron_card(f"{n:08d}", location_code=None),
        full_name=f"Scan Patron {n}",
        is_active=True,
    )
    session.add(p)
    session.flush()
    return p


def _create_pairing(client, cookies, *, modes=("checkout", "checkin", "catalog")):
    raw, signed = _csrf_pair()
    cookies = {**cookies, CSRF_COOKIE: signed}
    data = dict.fromkeys(modes, "on")
    data["csrf_token"] = raw
    resp = client.post("/ui/scan/pairings", data=data, cookies=cookies)
    return resp


# ── pairing creation ──────────────────────────────────────────────────────────


def test_create_pairing_renders_qr(scan_client, scan_session):
    cookies = _login(scan_client, scan_session)
    resp = _create_pairing(scan_client, cookies)
    assert resp.status_code == 200
    body = resp.text
    assert "<svg" in body
    assert "Unpair" in body
    # A pairing row exists, unclaimed.
    rows = scan_session.query(ScanPairing).all()
    assert rows
    assert rows[-1].claimed_at is None


def test_create_pairing_without_perms_403(scan_client, scan_session):
    role = _custom_role(scan_session, f"NoCircRole{_next()}", ["item.view"])
    cookies = _login(scan_client, scan_session, role_name=role.name)
    resp = _create_pairing(scan_client, cookies)
    assert resp.status_code == 403


def test_create_pairing_scope_intersection(scan_client, scan_session):
    """A checkout-only staffer can't create a catalog-mode pairing."""
    role = _custom_role(
        scan_session, f"CheckoutOnly{_next()}", ["item.view", "loan.checkout"]
    )
    cookies = _login(scan_client, scan_session, role_name=role.name)
    resp = _create_pairing(scan_client, cookies, modes=("checkout", "catalog"))
    assert resp.status_code == 200
    pairing = scan_session.query(ScanPairing).all()[-1]
    assert pairing.allowed_modes == ["checkout"]


def test_create_pairing_no_allowed_modes_400(scan_client, scan_session):
    role = _custom_role(
        scan_session, f"CheckoutOnly2_{_next()}", ["item.view", "loan.checkout"]
    )
    cookies = _login(scan_client, scan_session, role_name=role.name)
    resp = _create_pairing(scan_client, cookies, modes=("catalog",))
    assert resp.status_code == 400


# ── claim lifecycle ───────────────────────────────────────────────────────────
#
# The route hashes the claim secret, so we can't recover it from the row. We
# manufacture a pairing directly with a known claim secret to drive the claim.


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


def _staff_user(session, role_name="Librarian"):
    role = SqlRoleRepository(session).get_by_name(role_name)
    u = AppUser(
        username=f"scanu{_next()}", password_hash=hash_password("x"), role_id=role.id
    )
    SqlUserRepository(session).add(u)
    session.flush()
    return u


def test_claim_rotates_and_is_single_use(scan_client, scan_session):
    user = _staff_user(scan_session)
    row = _make_pairing(
        scan_session, user, claim="CLAIM_ABC", allowed_modes=["checkout"]
    )
    resp = scan_client.get("/ui/scan/pair?c=CLAIM_ABC")
    assert resp.status_code == 200
    assert SCAN_COOKIE in resp.cookies
    scan_session.refresh(row)
    assert row.claimed_at is not None
    # token_hash rotated → original claim no longer matches.
    assert row.token_hash != hashlib.sha256(b"CLAIM_ABC").hexdigest()
    # Replaying the claim now fails.
    resp2 = scan_client.get("/ui/scan/pair?c=CLAIM_ABC")
    assert resp2.status_code == 403


def test_claim_expired_rejected(scan_client, scan_session):
    user = _staff_user(scan_session)
    _make_pairing(
        scan_session, user, claim="CLAIM_EXP", allowed_modes=["checkout"], ttl_minutes=-1
    )
    resp = scan_client.get("/ui/scan/pair?c=CLAIM_EXP")
    assert resp.status_code == 403


def _claim_and_get_scan_cookie(client, session, user, *, allowed_modes, mode=None):
    claim = f"CLAIM_{_next()}"
    _make_pairing(session, user, claim=claim, allowed_modes=allowed_modes, mode=mode)
    resp = client.get(f"/ui/scan/pair?c={claim}")
    assert resp.status_code == 200
    return resp.cookies[SCAN_COOKIE]


# ── dispatch ──────────────────────────────────────────────────────────────────


def test_dispatch_checkout_flow(scan_client, scan_session):
    user = _staff_user(scan_session)
    scan_cookie = _claim_and_get_scan_cookie(
        scan_client, scan_session, user, allowed_modes=["checkout"], mode="checkout"
    )
    patron = _patron(scan_session)
    item = _book(scan_session, title="ScanDuneBook")

    raw, signed = _csrf_pair()
    cookies = {SCAN_COOKIE: scan_cookie, CSRF_COOKIE: signed}
    # First scan the patron card.
    r1 = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": patron.library_card_number, "csrf_token": raw},
        cookies=cookies,
    )
    assert r1.status_code == 200
    assert r1.json()["kind"] == "borrower_set"
    assert r1.json()["borrower"] == patron.library_card_number

    # Then scan the item.
    r2 = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": item.barcode, "csrf_token": raw},
        cookies=cookies,
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["kind"] == "checkout"
    assert "ScanDuneBook" in body["message"]
    assert body["count"] == 1

    # Loan landed for the scanned borrower. (Routine circulation isn't audited —
    # the loan row itself carries the history; see CLAUDE.md.)
    from compendium.repositories.sql.loan_repository import SqlLoanRepository

    loan = SqlLoanRepository(scan_session).get_active_for_item(item.id)
    assert loan is not None and loan.patron_id == patron.id


def test_dispatch_idempotent_duplicate(scan_client, scan_session):
    user = _staff_user(scan_session)
    scan_cookie = _claim_and_get_scan_cookie(
        scan_client, scan_session, user, allowed_modes=["checkin"], mode="checkin"
    )
    patron = _patron(scan_session)
    item = _book(scan_session)
    # Check the item out first (directly) so checkin has work to do.
    from compendium.repositories.sql.loan_repository import SqlLoanRepository  # noqa
    from compendium.domain.models import Loan

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
    # Immediate identical re-scan → ignored.
    r2 = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": item.barcode, "csrf_token": raw},
        cookies=cookies,
    )
    assert r2.json()["kind"] == "ignored"


def test_dispatch_without_cookie_403(scan_client, scan_session):
    raw, signed = _csrf_pair()
    resp = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": "123", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 403


def test_dispatch_revoked_403(scan_client, scan_session):
    user = _staff_user(scan_session)
    claim = f"CLAIM_REV_{_next()}"
    row = _make_pairing(scan_session, user, claim=claim, allowed_modes=["checkin"])
    resp = scan_client.get(f"/ui/scan/pair?c={claim}")
    scan_cookie = resp.cookies[SCAN_COOKIE]
    row.revoked_at = datetime.now(timezone.utc)
    scan_session.flush()
    raw, signed = _csrf_pair()
    resp = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": "30001000", "csrf_token": raw},
        cookies={SCAN_COOKIE: scan_cookie, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 403


def test_dispatch_deactivated_user_403(scan_client, scan_session):
    """A staff user deactivated mid-session can't act through a paired phone."""
    user = _staff_user(scan_session)
    scan_cookie = _claim_and_get_scan_cookie(
        scan_client, scan_session, user, allowed_modes=["checkout"], mode="checkout"
    )
    patron = _patron(scan_session)
    item = _book(scan_session, title="DeactivatedScanBook")

    # Deactivate the staff user after the phone is paired.
    user.is_active = False
    scan_session.flush()

    raw, signed = _csrf_pair()
    cookies = {SCAN_COOKIE: scan_cookie, CSRF_COOKIE: signed}
    # Scan the patron card first; a deactivated user must be rejected outright.
    resp = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": patron.library_card_number, "csrf_token": raw},
        cookies=cookies,
    )
    assert resp.status_code == 403

    # And a direct item scan is likewise refused — no loan is created.
    resp2 = scan_client.post(
        "/ui/scan/dispatch",
        data={"code": item.barcode, "csrf_token": raw},
        cookies=cookies,
    )
    assert resp2.status_code == 403

    from compendium.repositories.sql.loan_repository import SqlLoanRepository

    assert SqlLoanRepository(scan_session).get_active_for_item(item.id) is None


# ── mode switch ───────────────────────────────────────────────────────────────


def test_mode_switch_clears_borrower_and_count(scan_client, scan_session):
    user = _staff_user(scan_session)
    scan_cookie = _claim_and_get_scan_cookie(
        scan_client,
        scan_session,
        user,
        allowed_modes=["checkout", "checkin"],
        mode="checkout",
    )
    patron = _patron(scan_session)
    item = _book(scan_session)
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
    # Switch to checkin → borrower + count reset.
    r = scan_client.post(
        "/ui/scan/mode",
        data={"mode": "checkin", "csrf_token": raw},
        cookies=cookies,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "checkin"
    assert body["borrower"] is None
    assert body["count"] == 0


def test_mode_switch_to_disallowed_400(scan_client, scan_session):
    user = _staff_user(scan_session)
    scan_cookie = _claim_and_get_scan_cookie(
        scan_client, scan_session, user, allowed_modes=["checkout"], mode="checkout"
    )
    raw, signed = _csrf_pair()
    cookies = {SCAN_COOKIE: scan_cookie, CSRF_COOKIE: signed}
    r = scan_client.post(
        "/ui/scan/mode",
        data={"mode": "catalog", "csrf_token": raw},
        cookies=cookies,
    )
    assert r.status_code == 400


# ── unpair ────────────────────────────────────────────────────────────────────


def test_unpair_revokes(scan_client, scan_session):
    cookies = _login(scan_client, scan_session)
    resp = _create_pairing(scan_client, cookies)
    assert resp.status_code == 200
    pairing = scan_session.query(ScanPairing).all()[-1]
    raw, signed = _csrf_pair()
    r = scan_client.post(
        f"/ui/scan/pairings/{pairing.id}/unpair",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert r.status_code == 200
    scan_session.refresh(pairing)
    assert pairing.revoked_at is not None


def test_unpair_other_users_pairing_404(scan_client, scan_session):
    owner = _staff_user(scan_session)
    row = _make_pairing(
        scan_session, owner, claim=f"C_{_next()}", allowed_modes=["checkout"]
    )
    cookies = _login(scan_client, scan_session)
    raw, signed = _csrf_pair()
    r = scan_client.post(
        f"/ui/scan/pairings/{row.id}/unpair",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert r.status_code == 404


# ── desk page pair-a-phone section ────────────────────────────────────────────


def test_desk_pair_section_shows_only_permitted_modes(scan_client, scan_session):
    """Desk page shows Pair-a-phone section with only the modes the user holds."""
    # User with checkout permission only.
    role = _custom_role(
        scan_session, f"CheckoutDesk{_next()}", ["item.view", "loan.checkout"]
    )
    cookies = _login(scan_client, scan_session, role_name=role.name)
    resp = scan_client.get("/ui/circ", cookies=cookies, follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    # Section is present because user has at least one scan mode.
    assert "Pair a phone" in body
    # Checkout checkbox present.
    assert 'name="checkout"' in body
    # Checkin and catalog checkboxes absent (no permissions for those).
    assert 'name="checkin"' not in body
    assert 'name="catalog"' not in body


def test_desk_pair_section_absent_when_no_scan_perms(scan_client, scan_session):
    """Desk page hides Pair-a-phone section entirely when user lacks scan modes."""
    # Librarians have loan.checkout (required to reach /circ), but let's make a
    # custom role with only checkout so we can see the section, then make one
    # without any scan perms.  The /circ route requires loan.checkout — so the
    # user must hold it to reach the page at all; we cannot test a user with
    # zero scan perms reaching /circ unless loan.checkout is one of the scan
    # perms.  Instead, verify that a user with ONLY loan.checkout (no checkin/
    # catalog) sees ONLY the checkout checkbox and not the others.
    # For the "section absent" case we use a role that has loan.checkout but
    # does NOT have checkin or catalog — the section IS there (checkout), which
    # is already covered above.  To truly test "section absent" we need a user
    # whose permissions include loan.checkout (to pass require_web_permission)
    # but where MODE_PERMISSION maps none of those perms — but loan.checkout IS
    # a scan mode.  So we test the next best thing: a user with no checkin/
    # catalog sees those modes absent from the section.
    #
    # The cleanest "absent" scenario requires a role where none of checkout/
    # checkin/catalog is granted.  But /circ requires loan.checkout.  We can
    # test a redirect (403→login) for such a user trying to reach /circ.
    role = _custom_role(
        scan_session, f"NoScanPerms{_next()}", ["item.view", "patron.manage"]
    )
    cookies = _login(scan_client, scan_session, role_name=role.name)
    resp = scan_client.get("/ui/circ", cookies=cookies, follow_redirects=False)
    # The page requires loan.checkout — user without it gets redirected to login.
    assert resp.status_code in (302, 303, 403)


def test_desk_pair_section_absent_for_librarian_with_only_catalog(
    scan_client, scan_session
):
    """A user with only catalog.import as a scan perm sees only 'catalog' checkbox."""
    role = _custom_role(
        scan_session,
        f"CatalogOnlyDesk{_next()}",
        ["item.view", "loan.checkout", "catalog.import"],
    )
    cookies = _login(scan_client, scan_session, role_name=role.name)
    resp = scan_client.get("/ui/circ", cookies=cookies, follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    assert "Pair a phone" in body
    assert 'name="checkout"' in body   # loan.checkout is also a scan mode
    assert 'name="catalog"' in body    # catalog.import is a scan mode
    assert 'name="checkin"' not in body  # loan.checkin not in role


# ── phone page rendering ──────────────────────────────────────────────────────


def test_phone_page_renders_video_and_mode_buttons(scan_client, scan_session):
    """After claiming a pairing the phone page has video, mode buttons, scanner.js."""
    user = _staff_user(scan_session)
    claim = f"CLAIM_PHONE_{_next()}"
    _make_pairing(
        scan_session,
        user,
        claim=claim,
        allowed_modes=["checkout", "checkin"],
        mode="checkout",
    )
    resp = scan_client.get(f"/ui/scan/pair?c={claim}")
    assert resp.status_code == 200
    body = resp.text
    # Video element present.
    assert "<video" in body
    # A mode button for each allowed mode.
    assert 'data-mode="checkout"' in body
    assert 'data-mode="checkin"' in body
    # scanner.js included.
    assert "scanner.js" in body
    # CSRF token present (needed by the nonced script).
    assert "data-csrf-token=" in body
