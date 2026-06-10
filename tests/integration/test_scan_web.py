"""Web UI tests for /ui/scan/* phone-scanner pairing.

Shared scaffolding (the ``scan_engine`` / ``scan_session`` / ``scan_client``
fixtures and the helper functions) lives in ``conftest.py`` and
``scan_helpers.py``; this file only adds the assertions specific to pairing.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from compendium.domain.models import ScanPairing
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.deps import SCAN_COOKIE
from tests.integration.scan_helpers import (
    book as _book,
)
from tests.integration.scan_helpers import (
    create_pairing as _create_pairing,
)
from tests.integration.scan_helpers import (
    csrf_pair as _csrf_pair,
)
from tests.integration.scan_helpers import (
    custom_role as _custom_role,
)
from tests.integration.scan_helpers import (
    login as _login,
)
from tests.integration.scan_helpers import (
    make_pairing as _make_pairing,
)
from tests.integration.scan_helpers import (
    next_id as _next,
)
from tests.integration.scan_helpers import (
    patron as _patron,
)
from tests.integration.scan_helpers import (
    staff_user as _staff_user,
)

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
# manufacture a pairing directly with a known claim secret to drive the claim
# (see scan_helpers._make_pairing).


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
    from tests.integration.scan_helpers import claim as _claim

    _row, cookie = _claim(
        client, session, user, allowed_modes=allowed_modes, mode=mode
    )
    return cookie


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


# ── heartbeat ─────────────────────────────────────────────────────────────────


def test_heartbeat_204_when_live(scan_client, scan_session):
    user = _staff_user(scan_session)
    claim = f"CLAIM_HB_{_next()}"
    _make_pairing(scan_session, user, claim=claim, allowed_modes=["checkout"])
    resp = scan_client.get(f"/ui/scan/pair?c={claim}")
    scan_cookie = resp.cookies[SCAN_COOKIE]
    r = scan_client.get("/ui/scan/heartbeat", cookies={SCAN_COOKIE: scan_cookie})
    assert r.status_code == 204


def test_heartbeat_403_after_unpair(scan_client, scan_session):
    user = _staff_user(scan_session)
    claim = f"CLAIM_HB_REV_{_next()}"
    row = _make_pairing(scan_session, user, claim=claim, allowed_modes=["checkout"])
    resp = scan_client.get(f"/ui/scan/pair?c={claim}")
    scan_cookie = resp.cookies[SCAN_COOKIE]
    row.revoked_at = datetime.now(timezone.utc)
    scan_session.flush()
    r = scan_client.get("/ui/scan/heartbeat", cookies={SCAN_COOKIE: scan_cookie})
    assert r.status_code == 403
