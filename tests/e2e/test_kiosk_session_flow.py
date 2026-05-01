"""Kiosk self-checkout session flow.

The kiosk requires a user with `loan.checkout` permission (the kiosk device
runs as a dedicated user). Here we use the librarian page (Administrator
role = wildcard) to navigate the kiosk.

Verifies:
1. Entering a valid card number redirects to the session page.
2. The session page uses the stripped kiosk_base.html (no admin nav links).
3. An idle-timeout script is present on the session page.
4. Admin-only links are absent (kiosk chrome hides them).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_kiosk_session_redirect(librarian_page, e2e_server, e2e_seed):
    """Entering a valid patron card redirects to the kiosk session page."""
    page = librarian_page
    page.goto(f"{e2e_server}/ui/kiosk")
    page.wait_for_load_state("networkidle")

    page.fill("input[name=card_number]", e2e_seed.kiosk_card)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    assert f"/ui/kiosk/session/{e2e_seed.kiosk_card}" in page.url, (
        f"Expected redirect to kiosk session, got {page.url}"
    )


def test_kiosk_session_page_has_no_admin_links(librarian_page, e2e_server, e2e_seed):
    """Kiosk session page must not show admin navigation links."""
    page = librarian_page
    page.goto(f"{e2e_server}/ui/kiosk/session/{e2e_seed.kiosk_card}")
    page.wait_for_load_state("networkidle")

    # kiosk_base.html has no nav — admin links should be absent
    admin_links = page.locator("a[href*='/ui/admin']").all()
    assert len(admin_links) == 0, (
        f"Kiosk session page should not have admin links, found: "
        + str([a.get_attribute("href") for a in admin_links])
    )


def test_kiosk_session_page_has_idle_timeout_script(librarian_page, e2e_server, e2e_seed):
    """Kiosk session page must include the idle-timeout inline script."""
    page = librarian_page
    page.goto(f"{e2e_server}/ui/kiosk/session/{e2e_seed.kiosk_card}")
    page.wait_for_load_state("networkidle")

    # Verify the idle timeout JS variable was rendered into the page
    has_idle = page.evaluate("typeof window !== 'undefined'")  # basic JS sanity
    content = page.content()
    assert "idleMs" in content or "idle_timeout" in content.lower() or "setTimeout" in content, (
        "Kiosk session page is missing the idle-timeout script"
    )


def test_kiosk_invalid_card_shows_error(librarian_page, e2e_server):
    """An unrecognized card number shows an error and stays on the landing page."""
    page = librarian_page
    page.goto(f"{e2e_server}/ui/kiosk")
    page.wait_for_load_state("networkidle")

    page.fill("input[name=card_number]", "99999999")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    # Should redirect back to /ui/kiosk with an error message
    assert "/ui/kiosk" in page.url and "session" not in page.url, (
        f"Invalid card should stay on kiosk landing, got {page.url}"
    )
    content = page.content()
    assert "not recognized" in content.lower() or "error" in content.lower(), (
        "Expected an error message for unrecognized card"
    )
