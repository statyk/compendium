"""Integration tests for the Add-Item pairing panel + Edit-prefill round-trip.

Covers Task 7:
  - Part A: the shared "Pair a phone" partial mounts on the Add-Item page and
    the desk page still renders it (byte-identical extraction).
  - Part B: ``pending_id`` threads through the lookup → preview → create chain,
    the identifier is prefilled for the owner, and creating from an owned
    pending row resolves it (status approved + created_item_id).

Shared scaffolding lives in ``conftest.py`` / ``scan_helpers.py``.
"""

from __future__ import annotations

import pytest

from compendium.domain.models import AppUser, Item, ScanPendingItem
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from tests.integration.scan_helpers import csrf_pair as _csrf_pair
from tests.integration.scan_helpers import login as _login
from tests.integration.scan_helpers import make_pairing as _make_pairing
from tests.integration.scan_helpers import next_id as _next

_GIVER_ISBN = "9780544336261"
_GIVER_META = {"title": "The Giver", "isbn": _GIVER_ISBN}


@pytest.fixture(autouse=True)
def _stub_metadata(monkeypatch):
    """No network: stub upstream metadata for both the preview and create paths."""
    monkeypatch.setattr(
        "compendium.web.routes.items.lookup_metadata",
        lambda *a, **k: dict(_GIVER_META),
    )
    monkeypatch.setattr(
        "compendium.services.catalog.lookup_metadata",
        lambda *a, **k: dict(_GIVER_META),
    )
    monkeypatch.setattr(
        "compendium.services.catalog.lookup_cover_fallbacks",
        lambda *a, **k: None,
    )


def _login_owner(scan_client, scan_session, *, role_name="Librarian"):
    username = f"additemstaff{_next()}"
    cookies = _login(
        scan_client, scan_session, role_name=role_name, username=username
    )
    user = scan_session.query(AppUser).filter(AppUser.username == username).one()
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


# ── Part A: shared pair panel ────────────────────────────────────────────────


def test_add_item_page_renders_pair_panel(scan_client, scan_session):
    cookies, _owner = _login_owner(scan_client, scan_session)
    resp = scan_client.get("/ui/items/new", cookies=cookies)
    assert resp.status_code == 200
    assert "Generate pairing QR" in resp.text


def test_desk_still_renders_pair_panel(scan_client, scan_session):
    """The shared-partial extraction must not break the desk panel."""
    cookies, _owner = _login_owner(scan_client, scan_session)
    resp = scan_client.get("/ui/circ", cookies=cookies)
    assert resp.status_code == 200
    assert "Generate pairing QR" in resp.text


# ── Part B: prefill + round-trip ─────────────────────────────────────────────


def test_add_item_prefills_identifier_for_owner(scan_client, scan_session):
    """GET /ui/items/new?pending_id=<owned> prefills the identifier input
    with the snapshot ISBN and carries a hidden pending_id in the lookup form."""
    cookies, owner = _login_owner(scan_client, scan_session)
    pairing = _make_pairing(
        scan_session, owner, claim=f"C_{_next()}", allowed_modes=["catalog"]
    )
    pend = _pending_row(scan_session, pairing)

    resp = scan_client.get(
        f"/ui/items/new?pending_id={pend.id}", cookies=cookies
    )
    assert resp.status_code == 200
    body = resp.text
    # The identifier input is prefilled with the snapshot ISBN.
    assert f'value="{_GIVER_ISBN}"' in body
    # The lookup form carries the hidden pending_id carrier.
    assert f'name="pending_id" value="{pend.id}"' in body


def test_add_item_no_prefill_for_non_owner(scan_client, scan_session):
    """A different user passing A's pending_id sees neither prefill nor the
    hidden carrier (template-path confirmation of the Task-5 ownership gate)."""
    owner_cookies, owner = _login_owner(scan_client, scan_session)
    pairing = _make_pairing(
        scan_session, owner, claim=f"C_{_next()}", allowed_modes=["catalog"]
    )
    pend = _pending_row(scan_session, pairing)

    other_cookies, other = _login_owner(scan_client, scan_session)
    assert other.id != owner.id

    resp = scan_client.get(
        f"/ui/items/new?pending_id={pend.id}", cookies=other_cookies
    )
    assert resp.status_code == 200
    body = resp.text
    # No snapshot ISBN prefill, no hidden carrier.
    assert f'value="{_GIVER_ISBN}"' not in body
    assert f'name="pending_id" value="{pend.id}"' not in body


def test_edit_round_trip_closes_the_loop(scan_client, scan_session):
    """Owner drives the full lookup → preview → create chain; the preview
    carries the hidden pending_id and the create resolves the pending row."""
    cookies, owner = _login_owner(scan_client, scan_session)
    pairing = _make_pairing(
        scan_session, owner, claim=f"C_{_next()}", allowed_modes=["catalog"]
    )
    pend = _pending_row(scan_session, pairing)

    # Step 1: lookup with pending_id → preview must carry hidden pending_id.
    raw, signed = _csrf_pair()
    preview = scan_client.post(
        "/ui/items/lookup",
        data={
            "media_type": "book",
            "identifier": _GIVER_ISBN,
            "pending_id": str(pend.id),
            "csrf_token": raw,
        },
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert preview.status_code == 200
    assert f'name="pending_id" value="{pend.id}"' in preview.text

    # Step 2: create with the pending_id → item created, pending row resolved.
    items_before = scan_session.query(Item).count()
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
    assert resp.status_code in (200, 303)
    # (a) an Item was created.
    assert scan_session.query(Item).count() == items_before + 1
    # (b) the pending row is resolved.
    scan_session.refresh(pend)
    assert pend.status == "approved"
    assert pend.created_item_id is not None
    assert pend.resolved_by == owner.id
