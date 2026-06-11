"""Integration tests for the Add-Item pairing panel.

Covers:
  - The shared "Pair a phone" partial mounts on the Add-Item page and
    the desk page still renders it (byte-identical extraction).

Shared scaffolding lives in ``conftest.py`` / ``scan_helpers.py``.
"""

from __future__ import annotations

from compendium.domain.models import AppUser
from tests.integration.scan_helpers import login as _login
from tests.integration.scan_helpers import next_id as _next


def _login_owner(scan_client, scan_session, *, role_name="Librarian"):
    username = f"additemstaff{_next()}"
    cookies = _login(
        scan_client, scan_session, role_name=role_name, username=username
    )
    user = scan_session.query(AppUser).filter(AppUser.username == username).one()
    return cookies, user


# ── Shared pair panel ────────────────────────────────────────────────────────


def test_add_item_page_renders_pair_panel(scan_client, scan_session):
    """Add-Item offers all permitted modes but pre-checks only Catalog."""
    cookies, _owner = _login_owner(scan_client, scan_session)  # Librarian: all 3
    resp = scan_client.get("/ui/items/new", cookies=cookies)
    assert resp.status_code == 200
    assert "Generate pairing QR" in resp.text
    # All three modes are offered as checkboxes…
    assert 'name="checkout" value="on"' in resp.text
    assert 'name="checkin" value="on"' in resp.text
    assert 'name="catalog" value="on"' in resp.text
    # …but only Catalog is pre-checked on the cataloging page.
    assert 'name="catalog" value="on" checked>' in resp.text
    assert 'name="checkout" value="on">' in resp.text
    assert 'name="checkin" value="on">' in resp.text


def test_desk_still_renders_pair_panel(scan_client, scan_session):
    """The circ desk offers all permitted modes but pre-checks Checkout/Checkin."""
    cookies, _owner = _login_owner(scan_client, scan_session)  # Librarian: all 3
    resp = scan_client.get("/ui/circ", cookies=cookies)
    assert resp.status_code == 200
    assert "Generate pairing QR" in resp.text
    assert 'name="checkout" value="on"' in resp.text
    assert 'name="checkin" value="on"' in resp.text
    assert 'name="catalog" value="on"' in resp.text
    # Circulation modes pre-checked; Catalog available but unchecked.
    assert 'name="checkout" value="on" checked>' in resp.text
    assert 'name="checkin" value="on" checked>' in resp.text
    assert 'name="catalog" value="on">' in resp.text
