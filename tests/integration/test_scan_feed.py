"""Integration tests for scan_event emission on /ui/scan/dispatch.

Shared scaffolding lives in ``conftest.py`` / ``scan_helpers.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from compendium.domain.models import Loan, ScanEvent
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.deps import SCAN_COOKIE
from tests.integration.scan_helpers import book, patron, staff_user
from tests.integration.scan_helpers import claim as _claim
from tests.integration.scan_helpers import csrf_pair as _csrf_pair


def _staff_user(session, role_name="Librarian"):
    return staff_user(session, role_name, prefix="feedstaff")


def _book(session, title="Dune"):
    return book(session, title, acc_prefix="FACC")


def _patron(session):
    return patron(session, name_prefix="Feed Patron")


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
