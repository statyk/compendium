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
