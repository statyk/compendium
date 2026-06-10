"""Integration tests for review-first catalog scanning (/ui/scan/*).

Shared scaffolding lives in ``conftest.py`` / ``scan_helpers.py``.
"""

from __future__ import annotations

import pytest

from compendium.domain.models import Item, ScanPendingItem
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.deps import SCAN_COOKIE
from tests.integration.scan_helpers import claim as _claim
from tests.integration.scan_helpers import csrf_pair as _csrf_pair
from tests.integration.scan_helpers import staff_user

_GIVER_ISBN = "9780544336261"
_GIVER_META = {"title": "The Giver", "isbn": _GIVER_ISBN}
# A second ISBN for the review-off test, so it can't collide with the review-on
# test on the per-process idempotency guard (keyed on pairing+mode+code).
_OFF_ISBN = "9780571056866"


def _staff_user(session, role_name="Librarian"):
    return staff_user(session, role_name, prefix="revstaff")


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
