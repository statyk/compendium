"""Integration tests for review-first catalog scanning (/ui/scan/*).

Shared scaffolding lives in ``conftest.py`` / ``scan_helpers.py``.
"""

from __future__ import annotations

import pytest

from compendium.domain.models import AppUser, Item, ScanEvent, ScanPendingItem
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.deps import SCAN_COOKIE
from tests.integration.scan_helpers import claim as _claim
from tests.integration.scan_helpers import csrf_pair as _csrf_pair
from tests.integration.scan_helpers import login as _login
from tests.integration.scan_helpers import make_pairing as _make_pairing
from tests.integration.scan_helpers import next_id as _next
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


# ── desk approve / discard / log (staff web session, not the phone) ───────────


def _login_owner(scan_client, scan_session, *, role_name="Librarian"):
    """Log in a fresh staff user and return ``(cookies, user)``."""
    username = f"deskstaff{_next()}"
    cookies = _login(
        scan_client, scan_session, role_name=role_name, username=username
    )
    user = (
        scan_session.query(AppUser).filter(AppUser.username == username).one()
    )
    return cookies, user


def _pending_row(scan_session, pairing):
    pend = ScanPendingItem(
        pairing_id=pairing.id,
        isbn=_GIVER_ISBN,
        title="The Giver",
        meta_json=dict(_GIVER_META),
        cover_url=None,
        status="pending",
    )
    scan_session.add(pend)
    scan_session.flush()
    return pend


def test_qr_partial_retargets_approve_discard_at_inner_poll_div(scan_session):
    """The poll-loop <div> must carry id="scan-activity-<id>" and the
    approve/discard forms must target it (not the outer wrapper), so a swap
    leaves the poll loop + Unpair button in the DOM."""
    from compendium.web.jinja import templates

    user = _staff_user(scan_session)
    pairing = _make_pairing(
        scan_session, user, claim=f"QRP{_next()}", allowed_modes=["catalog"]
    )
    pend = _pending_row(scan_session, pairing)

    html = templates.get_template("scan/_qr_partial.html").render(
        request=None,
        pairing=pairing,
        qr_svg="<svg></svg>",
        csrf_token="t",
        events=[],
        pending=[pend],
    )
    inner_id = f'id="scan-activity-{pairing.id}"'
    target = f'hx-target="#scan-activity-{pairing.id}"'
    # The persistent poll div carries the inner id and the every-1500ms trigger.
    assert inner_id in html
    assert 'hx-trigger="every 1500ms"' in html
    # Both action forms target the inner poll container, not the outer wrapper.
    assert html.count(target) == 2
    # The only remaining outer-wrapper target is the Unpair form (outerHTML swap).
    assert html.count(f'hx-target="#scan-pairing-{pairing.id}"') == 1


def test_approve_creates_item_and_removes_pending(scan_client, scan_session):
    cookies, owner = _login_owner(scan_client, scan_session)
    pairing = _make_pairing(
        scan_session, owner, claim=f"C_{_next()}", allowed_modes=["catalog"]
    )
    pend = _pending_row(scan_session, pairing)

    items_before = scan_session.query(Item).count()
    raw, signed = _csrf_pair()
    resp = scan_client.post(
        f"/ui/scan/pairings/{pairing.id}/pending/{pend.id}/approve",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    # An Item was created from the snapshot.
    assert scan_session.query(Item).count() == items_before + 1
    scan_session.refresh(pend)
    assert pend.status == "approved"
    assert pend.created_item_id is not None
    assert pend.resolved_at is not None
    assert pend.resolved_by == owner.id
    # The approved row is no longer listed in the returned feed/queue.
    assert "The Giver" not in resp.text or "Catalogued" in resp.text
    # And it definitely isn't in the pending queue section anymore.
    assert f"/pending/{pend.id}/approve" not in resp.text


def test_discard_marks_discarded_no_item(scan_client, scan_session):
    cookies, owner = _login_owner(scan_client, scan_session)
    pairing = _make_pairing(
        scan_session, owner, claim=f"C_{_next()}", allowed_modes=["catalog"]
    )
    pend = _pending_row(scan_session, pairing)

    items_before = scan_session.query(Item).count()
    raw, signed = _csrf_pair()
    resp = scan_client.post(
        f"/ui/scan/pairings/{pairing.id}/pending/{pend.id}/discard",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 200
    scan_session.refresh(pend)
    assert pend.status == "discarded"
    assert pend.created_item_id is None
    # No Item was created.
    assert scan_session.query(Item).count() == items_before
    assert f"/pending/{pend.id}/discard" not in resp.text


def test_approve_other_users_pairing_rejected(scan_client, scan_session):
    owner = _staff_user(scan_session)
    pairing = _make_pairing(
        scan_session, owner, claim=f"C_{_next()}", allowed_modes=["catalog"]
    )
    pend = _pending_row(scan_session, pairing)

    # A different staff user logs in and tries to approve.
    cookies, _other = _login_owner(scan_client, scan_session)
    items_before = scan_session.query(Item).count()
    raw, signed = _csrf_pair()
    resp = scan_client.post(
        f"/ui/scan/pairings/{pairing.id}/pending/{pend.id}/approve",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code in (403, 404)
    scan_session.refresh(pend)
    assert pend.status == "pending"
    assert scan_session.query(Item).count() == items_before


def test_discard_other_users_pairing_rejected(scan_client, scan_session):
    owner = _staff_user(scan_session)
    pairing = _make_pairing(
        scan_session, owner, claim=f"C_{_next()}", allowed_modes=["catalog"]
    )
    pend = _pending_row(scan_session, pairing)

    cookies, _other = _login_owner(scan_client, scan_session)
    raw, signed = _csrf_pair()
    resp = scan_client.post(
        f"/ui/scan/pairings/{pairing.id}/pending/{pend.id}/discard",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code in (403, 404)
    scan_session.refresh(pend)
    assert pend.status == "pending"


def _capture_new_form_ctx(scan_client, monkeypatch, *, cookies, pending_id):
    """Drive GET /ui/items/new and capture the template context dict.

    ``items/new.html`` does not (yet) render ``prefill``/``pending_id``, so the
    only observable difference between an owner and a non-owner is in the route
    context. We capture it here by intercepting the template render.
    """
    from compendium.web.jinja import templates

    captured: dict = {}
    orig = templates.TemplateResponse

    def _spy(request, name, context=None, *args, **kwargs):
        if name == "items/new.html":
            captured.update(context or {})
        return orig(request, name, context, *args, **kwargs)

    monkeypatch.setattr(templates, "TemplateResponse", _spy)
    resp = scan_client.get(
        f"/ui/items/new?pending_id={pending_id}", cookies=cookies
    )
    assert resp.status_code == 200
    return captured


def test_add_item_prefill_does_not_leak_across_users(
    scan_client, scan_session, monkeypatch
):
    """IDOR: GET /ui/items/new?pending_id=<A's> must not prefill for user B.

    User A owns a pending scan. A different staff user B (also holding
    ``catalog.import``) requests the Add-Item form with A's ``pending_id``.
    B must be treated as if no ``pending_id`` was passed: the route context
    carries neither A's snapshot (``prefill``) nor the ``pending_id`` carrier.
    """
    owner_cookies, owner = _login_owner(scan_client, scan_session)
    pairing = _make_pairing(
        scan_session, owner, claim=f"C_{_next()}", allowed_modes=["catalog"]
    )
    pend = _pending_row(scan_session, pairing)

    # B is a different staff user who also holds catalog.import.
    other_cookies, other = _login_owner(scan_client, scan_session)
    assert other.id != owner.id

    # Positive control — owner A: gate open → prefill + pending_id present.
    owner_ctx = _capture_new_form_ctx(
        scan_client, monkeypatch, cookies=owner_cookies, pending_id=pend.id
    )
    assert owner_ctx.get("pending_id") == pend.id
    assert owner_ctx.get("prefill") == dict(_GIVER_META)

    # Non-owner B: gate closed → no prefill, no pending_id.
    other_ctx = _capture_new_form_ctx(
        scan_client, monkeypatch, cookies=other_cookies, pending_id=pend.id
    )
    assert other_ctx.get("pending_id") is None
    assert other_ctx.get("prefill") is None


def test_add_item_create_does_not_consume_other_users_pending(
    scan_client, scan_session
):
    """IDOR (write): B posting A's pending_id must not approve A's pending row."""
    owner = _staff_user(scan_session)
    pairing = _make_pairing(
        scan_session, owner, claim=f"C_{_next()}", allowed_modes=["catalog"]
    )
    pend = _pending_row(scan_session, pairing)

    cookies, other = _login_owner(scan_client, scan_session)
    assert other.id != owner.id

    raw, signed = _csrf_pair()
    resp = scan_client.post(
        "/ui/items/new",
        data={
            "media_type": "book",
            "identifier_kind": "isbn",
            "identifier_value": _GIVER_ISBN,
            "pending_id": str(pend.id),
            "csrf_token": raw,
        },
        cookies={**cookies, CSRF_COOKIE: signed},
        follow_redirects=False,
    )
    # The create itself may succeed (B is allowed to add items); what must NOT
    # happen is consuming A's pending row.
    assert resp.status_code in (200, 303)
    scan_session.refresh(pend)
    assert pend.status == "pending"
    assert pend.created_item_id is None
    assert pend.resolved_by is None


def test_log_renders_feed_and_pending(scan_client, scan_session):
    cookies, owner = _login_owner(scan_client, scan_session)
    pairing = _make_pairing(
        scan_session, owner, claim=f"C_{_next()}", allowed_modes=["catalog"]
    )
    pairing.claimed_at = pairing.created_at
    scan_session.add(
        ScanEvent(
            pairing_id=pairing.id,
            mode="catalog",
            kind="ok",
            message="Queued for review: The Giver",
        )
    )
    _pending_row(scan_session, pairing)
    scan_session.flush()

    resp = scan_client.get(
        f"/ui/scan/pairings/{pairing.id}/log", cookies=cookies
    )
    assert resp.status_code == 200
    body = resp.text
    # The live activity feed shows the recent scan_event message.
    assert "Queued for review: The Giver" in body
    # The review queue lists the pending title.
    assert "The Giver" in body
